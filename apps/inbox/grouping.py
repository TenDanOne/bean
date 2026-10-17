"""Tying a comment back to the post it was left on.

`InboxMessage.related_post` has existed for a long time and was filled on zero
of the rows, so the inbox was a flat list of remarks with no way to tell which
publication any of them was about. Answering a comment meant opening the
platform in another tab to see what the person was replying to.

Providers already carry the id — YouTube and Facebook under `extra["video_id"]`,
Pinterest under `extra["media_id"]` — and the publisher records the same value on
`PlatformPost.platform_post_id` when a post goes out. Nothing was joining them.

Two consequences worth knowing:

  * Only posts published *through this app* can be matched. A comment on a video
    uploaded directly on YouTube has no PlatformPost to point at, and that is
    not a bug — grouping degrades to "not from a post we made", which is honest.
  * The match is scoped to the message's own channel. Platform ids are only
    unique per platform, and two workspaces could hold the same numeric id.
"""

from __future__ import annotations

# Keys the providers use for "the post this comment is attached to". Checked in
# order; the first one present wins.
POST_ID_KEYS = ("video_id", "media_id", "post_id", "object_id")


def extract_platform_post_id(message) -> str | None:
    """The platform's id for the post this message belongs to, if any."""
    extra = getattr(message, "extra", None) or {}
    for key in POST_ID_KEYS:
        value = extra.get(key)
        if value:
            return str(value)
    return None


def resolve_related_post(message, cache: dict | None = None):
    """Find the ``PlatformPost`` this message was left on, or None.

    Pass ``cache`` when resolving a batch — a sync pulls hundreds of comments
    that mostly belong to a handful of posts, and without it this is one query
    per comment.
    """
    from apps.composer.models import PlatformPost

    platform_post_id = extract_platform_post_id(message)
    if not platform_post_id or not message.social_account_id:
        return None

    key = (message.social_account_id, platform_post_id)
    if cache is not None and key in cache:
        return cache[key]

    # Scoped to the channel: a platform id is unique per platform, not globally,
    # so matching on the id alone could attach a comment to another workspace's
    # post.
    post = PlatformPost.objects.filter(
        social_account_id=message.social_account_id,
        platform_post_id=platform_post_id,
    ).first()
    if cache is not None:
        cache[key] = post
    return post


def attach_related_posts(messages) -> int:
    """Fill ``related_post`` on a batch of saved messages. Returns how many.

    Only touches rows that have none, so re-running is safe and cheap — the
    backfill command and every sync both call this.
    """
    cache: dict = {}
    updated = []
    for message in messages:
        if message.related_post_id:
            continue
        post = resolve_related_post(message, cache)
        if post is not None:
            message.related_post = post
            updated.append(message)
    if updated:
        type(updated[0]).objects.bulk_update(updated, ["related_post"])
    return len(updated)


def post_url(message) -> str | None:
    """A link to the publication on its own platform.

    Free — built from the id we already hold — and it is what makes a folder
    usable when we have no title for it. Only the platforms whose URL shape is
    stable enough to construct are here; the rest get no link rather than a
    broken one.
    """
    platform_post_id = extract_platform_post_id(message)
    if not platform_post_id:
        return None
    platform = getattr(getattr(message, "social_account", None), "platform", "")
    if platform in ("youtube",):
        return f"https://www.youtube.com/watch?v={platform_post_id}"
    if platform in ("facebook",):
        return f"https://www.facebook.com/{platform_post_id}"
    return None


def group_by_post(messages, oldest_first=False):
    """Group messages into publications, newest activity first.

    Keyed on the platform's own post id from ``extra``, **not** on
    ``related_post``. That matters more than it looks: `related_post` only
    exists for posts published through this app, and a channel connected to an
    account with years of existing videos has none — every comment would land in
    one undifferentiated pile, which is the opposite of the point.

    ``related_post`` still gets used when it is there, because it carries the
    caption we wrote and is a better folder name than a video id.

    Each group is a dict: ``key``, ``post`` (PlatformPost or None), ``label``,
    ``url``, ``messages``, ``unread``, ``latest``.
    """
    buckets: dict = {}
    for message in messages:
        platform_post_id = extract_platform_post_id(message)
        key = platform_post_id or "unmatched"
        bucket = buckets.setdefault(
            key,
            {
                "key": key,
                "platform_post_id": platform_post_id,
                "post": None,
                "url": None,
                # Which network this publication lives on. A folder list mixing
                # YouTube and Instagram with the same generic icon makes you
                # read every title to know where you are.
                "platform": getattr(getattr(message, "social_account", None), "platform", ""),
                "messages": [],
                "unread": 0,
                "latest": None,
            },
        )
        bucket["messages"].append(message)
        if message.related_post_id and bucket["post"] is None:
            bucket["post"] = message.related_post
        if bucket["url"] is None:
            bucket["url"] = post_url(message)
        # `status`, not a boolean: a message is unread, open or resolved, and
        # "not resolved" is not the same question as "not read".
        if message.status == type(message).Status.UNREAD:
            bucket["unread"] += 1
        if bucket["latest"] is None or message.received_at > bucket["latest"]:
            bucket["latest"] = message.received_at

    groups = list(buckets.values())
    for group in groups:
        group["label"] = _folder_label(group)
        # Ids for the bulk bar, so ticking a folder feeds the same selection
        # Set the per-message checkboxes already use — one selection mechanism,
        # not two that can disagree about what is selected.
        group["message_ids"] = [str(m.id) for m in group["messages"]]
        # When the publication went out, not when someone commented on it —
        # "2 comments" on a video from last week means something different from
        # the same two on one from two years ago.
        group["published_at"] = _published_at(group["messages"])

    # The catch-all sinks either way: it is a bucket, not a publication.
    #
    # Oldest-first orders folders by their *oldest* unanswered comment, not
    # their newest — sorting by newest while claiming to show oldest first would
    # put a folder with one fresh comment above one that has been waiting weeks.
    def key(group):
        stamps = [m.received_at for m in group["messages"]]
        edge = min(stamps) if (oldest_first and stamps) else group["latest"]
        seconds = edge.timestamp() if edge else 0
        return (group["platform_post_id"] is None, seconds if oldest_first else -seconds)

    groups.sort(key=key)
    return groups


def _folder_label(group) -> str:
    """Best name we have for the folder, in descending order of usefulness.

    A caption we wrote beats a title we fetched beats the raw id. The id is a
    poor label but an honest one, and it is still enough to tell two folders
    apart — which is the minimum a folder has to do.
    """
    post = group["post"]
    if post is not None:
        caption = (getattr(post.post, "caption", "") or "").strip()
        if caption:
            return (caption[:70] + "…") if len(caption) > 70 else caption
    # Stored by sync when the platform can tell us; see fetch_post_titles.
    for message in group["messages"]:
        title = (message.extra or {}).get("post_title")
        if title:
            return title
    if group["platform_post_id"]:
        return group["platform_post_id"]
    return "Not from a post we can identify"


def resolve_related_post_for(account, provider_message, cache: dict | None = None):
    """``resolve_related_post`` for a provider message not yet saved.

    Sync builds the row and saves in one ``update_or_create``, so the lookup has
    to happen before there is an ``InboxMessage`` to pass.
    """
    from apps.composer.models import PlatformPost

    extra = getattr(provider_message, "extra", None) or {}
    platform_post_id = None
    for key in POST_ID_KEYS:
        if extra.get(key):
            platform_post_id = str(extra[key])
            break
    if not platform_post_id:
        return None

    cache_key = (account.id, platform_post_id)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    post = PlatformPost.objects.filter(social_account=account, platform_post_id=platform_post_id).first()
    if cache is not None:
        cache[cache_key] = post
    return post


def label_for(message) -> str | None:
    """The publication name to show under a single message.

    Same precedence as the folder headings — a caption we wrote, then a title we
    fetched, then the raw id — so a comment is never called one thing in the
    folder list and another in search results.

    Returns None when there is nothing to say, which is the right answer for a
    direct message rather than a label reading "no publication".
    """
    post = message.related_post if message.related_post_id else None
    if post is not None:
        caption = (getattr(post.post, "caption", "") or "").strip()
        if caption:
            return (caption[:60] + "…") if len(caption) > 60 else caption
    title = (message.extra or {}).get("post_title")
    if title:
        return title
    return extract_platform_post_id(message)


def _published_at(messages):
    """When the publication itself went out, as a datetime, or None.

    Stored as an ISO string by the sync because it comes straight off the API;
    parsed here so templates can use the usual date filters rather than each
    one reformatting a string.
    """
    from django.utils.dateparse import parse_datetime

    for message in messages:
        raw = (message.extra or {}).get("post_published_at")
        if raw:
            parsed = parse_datetime(raw)
            if parsed:
                return parsed
    return None
