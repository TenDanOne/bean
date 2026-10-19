"""Views for the Unified Social Inbox (F-3.1)."""

import logging
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.members.channels import can_on_channel, visible_accounts
from apps.members.decorators import require_channel_capability, require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers import get_provider
from providers.exceptions import APIError, RateLimitError

from .forms import (
    AssignForm,
    BulkActionForm,
    InternalNoteForm,
    ReplyForm,
    SavedReplyForm,
    SentimentForm,
    SLAConfigForm,
    StatusForm,
)
from .grouping import extract_platform_post_id, group_by_post, label_for
from .models import (
    InboxMessage,
    InboxReply,
    InboxSLAConfig,
    InternalNote,
    SavedReply,
)

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 50

# Named windows for the date filter. "Last 7 days" is the question people ask;
# making them pick two dates to express it is three interactions for one thought.
DATE_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}

# How many messages to read when building the folder list. Folders are grouped
# in Python because the post id lives under a different key per platform
# (`video_id`, `media_id`), so there is no one JSON lookup that finds them all.
# A bound is still needed: without one, a workspace with 50,000 comments would
# load all of them to draw a sidebar. 2,000 covers any realistic inbox and the
# tail is older than anyone scrolls to.
FOLDER_SCAN_LIMIT = 2000

# "Hidden" and "Deleted" are offered inside the status dropdown, but they are
# not statuses: they are moderation flags on the same row, and the Status
# column only holds unread/open/resolved/archived. The view splits them out
# before filtering against that column.
MODERATION_STATUS_VALUES = frozenset({"hidden", "deleted"})


def _avatar_url(user):
    """A user's avatar URL, or "" when they have none.

    Not `getattr(user.avatar, "url", "")`: an empty FileField *raises*
    ValueError on `.url` rather than being absent, so the default never applies
    and the whole panel 500s for anyone who never uploaded a picture.
    """
    avatar = getattr(user, "avatar", None)
    if not avatar:
        return ""
    try:
        return avatar.url
    except ValueError:
        return ""


def _turn_from_message(message):
    """One synced message as a conversation turn.

    Used for the root as well as for every child, so the message being answered
    is drawn by exactly the same code as the replies under it. Rendering it
    separately is how it ended up as the one bubble in the panel with no face.
    """
    return {
        "side": "right" if message.is_outbound else "left",
        "kind": "message",
        "pending": False,
        "name": message.sender_name,
        "handle": message.sender_handle,
        "avatar_url": message.sender_avatar_url,
        "initial": (message.sender_name or "?")[0].upper(),
        "body": message.body,
        "at": message.received_at,
        "sent_by": message.sent_by,
        "platform": message.social_account.get_platform_display(),
        # The row itself, so a bubble can carry the same moderation controls the
        # panel header has. Replies we wrote only ever appear here — the feed
        # excludes outbound messages — so without this there is nowhere in the
        # product to take back something we posted.
        "message": message,
    }


def _thread_turns(root, replies, notes, children):
    """The conversation as uniform turns, oldest first.

    Normalised here rather than in the template because the template was doing
    the deciding — three partials, each resolving "who is this from" its own way,
    which is how they drifted into three visual languages. The worst of it was
    invisible: a reply you had just sent rendered through one template and,
    once the platform confirmed it, through another, so the same message changed
    shape underneath you.

    Every turn is the same shape:

        side       "right" ours, "left" theirs, "none" internal notes
        pending    sent from here, not yet confirmed by the platform
        author     the public identity — the channel for us, the person for them
        sent_by    the team member behind an outbound reply, when we know
    """
    turns = []

    for reply in replies:
        # Sent from here and not yet echoed back by sync: ours, still in flight.
        turns.append(
            {
                "side": "right",
                "kind": "reply",
                "pending": True,
                "name": reply.author.display_name,
                "handle": "",
                "avatar_url": _avatar_url(reply.author),
                "initial": (reply.author.display_name or "?")[0].upper(),
                "body": reply.body,
                "at": reply.sent_at,
                "sent_by": reply.author,
            }
        )

    for note in notes:
        turns.append(
            {
                # Not a turn in the conversation — never sent, never seen by
                # them. A side would imply it was.
                "side": "none",
                "kind": "note",
                "pending": False,
                "name": note.author.display_name,
                "handle": "",
                "avatar_url": _avatar_url(note.author),
                "initial": (note.author.display_name or "?")[0].upper(),
                "body": note.body,
                "at": note.created_at,
                "sent_by": None,
            }
        )

    # Same builder as the root: one definition of what a message turn looks
    # like, so the two can never disagree.
    turns.extend(_turn_from_message(child) for child in children)

    return sorted(turns, key=lambda t: t["at"])


def _detail_context(workspace, message):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    saved_replies = SavedReply.objects.for_workspace(workspace.id)
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    # One chronological thread. Child messages used to render in a separate
    # block from replies, which split a single conversation in two and showed
    # the same reply twice — once as the local InboxReply, once as the comment
    # the API handed back. Sync now folds those together, so anything still in
    # `replies` is a send the platform hasn't confirmed yet.
    replies = list(message.replies.select_related("author"))
    notes = list(message.internal_notes.select_related("author"))
    children = list(InboxMessage.objects.filter(parent_message=message).select_related("social_account", "sent_by"))
    thread = _thread_turns(message, replies, notes, children)
    root_turn = _turn_from_message(message)
    return {
        "workspace": workspace,
        "message": message,
        "thread": thread,
        "root_turn": root_turn,
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
        "sentiment_choices": InboxMessage.Sentiment.choices,
    }


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Se requiere autenticación.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("No eres miembro de este espacio de trabajo.")
    return workspace


# --- Main Feed ---


def _filter_querystring(request):
    """The active filters as a query string fragment, minus the folder itself.

    So a link into a folder keeps the window, the sort and the search that were
    already applied. `post` is dropped because the link supplies its own, and
    blank values are dropped because the filter bar submits `?platform=&status=`
    when its selects sit on "All".
    """
    from urllib.parse import urlencode

    pairs = [
        (key, value) for key in request.GET if key != "post" for value in request.GET.getlist(key) if value.strip()
    ]
    return ("&" + urlencode(pairs)) if pairs else ""


def _filter_values(request, key):
    """Multi-value query param with the blank "All …" sentinel stripped out."""
    return [value for value in request.GET.getlist(key) if value]


@login_required
@require_permission("use_inbox")
def inbox_feed(request, workspace_id):
    """Main inbox feed with filtering, pagination, and split-panel layout."""
    workspace = _get_workspace(request, workspace_id)

    # Only what actually needs answering: top-level messages from other people.
    # Replies live inside their parent's thread, and anything the connected
    # account said itself is our own voice coming back through sync.
    # Narrowed to the channels this member was assigned. An owner sees all of
    # them; everyone else sees exactly what they were given, so a reviewer with
    # one channel reads only their own comments.
    qs = (
        InboxMessage.objects.for_workspace(workspace.id)
        .filter(parent_message__isnull=True, is_outbound=False)
        .filter(social_account__in=visible_accounts(request.workspace_membership))
        .select_related("social_account", "assigned_to")
    )

    # View shortcuts
    view = request.GET.get("view", "all")
    if view == "mine":
        qs = qs.filter(assigned_to=request.user)
    elif view == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)

    # Filters. Every "All X" option submits an empty value, and the filter bar
    # includes each control in the others' requests — so getlist() hands back
    # [""], which is truthy and used to filter on platform__in=[""], emptying the
    # inbox the moment any filter or the search box was touched. Drop the blanks.
    platforms = _filter_values(request, "platform")
    if platforms:
        qs = qs.filter(social_account__platform__in=platforms)

    accounts = _filter_values(request, "account")
    if accounts:
        qs = qs.filter(social_account_id__in=accounts)

    types = _filter_values(request, "type")
    if types:
        qs = qs.filter(message_type__in=types)

    statuses = _filter_values(request, "status")
    moderation_states = {s for s in statuses if s in MODERATION_STATUS_VALUES}
    real_statuses = [s for s in statuses if s not in moderation_states]
    if real_statuses:
        qs = qs.filter(status__in=real_statuses)

    # Moderation state. Hidden and deleted rows stay in the database on purpose
    # (a hidden one must be restorable, a deleted one keeps the thread and the
    # audit trail) — which is also why they looked exactly like untouched rows,
    # with no way to confirm a comment had actually been dealt with. The two
    # "statuses" are that check: they list precisely what moderation actions
    # left behind. No restoration, only the audit view.
    if "hidden" in moderation_states:
        qs = qs.filter(is_hidden=True)
    if "deleted" in moderation_states:
        qs = qs.filter(deleted_on_platform__isnull=False)

    assigned = request.GET.get("assigned")
    if assigned:
        qs = qs.filter(assigned_to__isnull=True) if assigned == "unassigned" else qs.filter(assigned_to_id=assigned)

    sentiments = _filter_values(request, "sentiment")
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    # A named window, or explicit dates. The presets exist because "last 7 days"
    # is the question people actually ask, and making them pick two dates for it
    # is three interactions to express one thought.
    #
    # An explicit date_from wins: if someone typed a date, honouring a preset
    # over it would silently discard what they asked for.
    date_range = request.GET.get("range", "").strip()
    date_from = request.GET.get("date_from")
    if not date_from and date_range in DATE_RANGE_DAYS:
        date_from = (timezone.now() - timedelta(days=DATE_RANGE_DAYS[date_range])).date().isoformat()
    if date_from:
        qs = qs.filter(received_at__date__gte=date_from)

    date_to = request.GET.get("date_to")
    if date_to:
        qs = qs.filter(received_at__date__lte=date_to)

    # Oldest first is not a novelty: working a backlog from the bottom means
    # answering the person who has been waiting longest, which is the whole
    # point of the SLA the inbox already tracks.
    sort = "oldest" if request.GET.get("sort") == "oldest" else "newest"
    qs = qs.order_by("received_at" if sort == "oldest" else "-received_at")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))

    # Folders, or the inside of one.
    #
    # `?post=` decides the depth, and it is one view rather than two because
    # these are the same filtered list looked at from different heights — the
    # filters, the channel scoping and the ordering all have to behave
    # identically, and two views would drift.
    #
    # Grouping happens *after* every filter, so a folder's count always matches
    # what is inside it. A count that disagrees with the list below it is worse
    # than no count.
    active_post_id = request.GET.get("post", "").strip()
    folders, active_folder = [], None

    # Which filters keep the publication folders, and which flatten the list.
    #
    # The distinction is not "filtered or not" — that was the first attempt and
    # it was too blunt. Narrowing to unread, or to one platform, leaves the
    # publications intact and the folders genuinely more useful: they answer
    # "which video has the most left to answer" at a glance. So those keep
    # folders, with each count reflecting only what passed the filter.
    #
    # Three cases go flat, each for its own reason:
    #   - a text search is a hunt for specific words, and folders would mean
    #     opening twenty of them to read twenty matches;
    #   - My Queue and Unassigned are a to-do list, not a place to browse;
    #   - filtering to direct messages only, because a DM belongs to no
    #     publication and every one of them would pile into a single catch-all.
    only_dms = bool(types) and set(types) == {"dm"}
    is_flat = bool(q) or view != "all" or only_dms

    if active_post_id:
        # Matched in Python for the same reason the folders are: the id key
        # differs by platform, so there is no single ORM lookup for it.
        messages = [m for m in qs[:FOLDER_SCAN_LIMIT] if extract_platform_post_id(m) == active_post_id][
            :MESSAGES_PER_PAGE
        ]
        active_folder = next((g for g in group_by_post(messages) if g["key"] == active_post_id), None)
    elif is_flat:
        messages = list(qs[:MESSAGES_PER_PAGE])
    else:
        # The folder list needs the whole filtered set to count correctly, not
        # one page of it — a folder showing "3 of 12" because the rest fell off
        # the page is a lie the user cannot see.
        folders = group_by_post(list(qs[:FOLDER_SCAN_LIMIT]), oldest_first=(sort == "oldest"))
        messages = []

    if is_flat and not only_dms:
        for message in messages:
            message.post_label = label_for(message)

    # SLA config for countdown display
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()

    # Team members for assignment dropdown
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")

    # Connected accounts for filter dropdown
    # The filter must offer the same channels the list can show, or picking one
    # silently returns nothing.
    social_accounts = visible_accounts(
        request.workspace_membership,
        SocialAccount.objects.filter(
            workspace=workspace,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ),
    )

    context = {
        "workspace": workspace,
        "inbox_messages": messages,
        "folders": folders,
        "active_folder": active_folder,
        "active_post_id": active_post_id,
        # The template needs to know which of the two lists it is drawing, and
        # "searching" is not the same question as "inside a folder".
        "showing_folders": not active_post_id and not is_flat,
        # Everything currently narrowing the list, ready to append to a link.
        #
        # Opening a folder used to be a bare `?post=<id>`, which threw away every
        # active filter — so "last 3 months" narrowed the folder list and then
        # the folder itself showed comments from years back. The filter appeared
        # broken when it was the navigation losing it.
        "filter_qs": _filter_querystring(request),
        # A flat result still says which publication each comment came from —
        # finding the words is only half of knowing what they were about.
        "show_post_label": is_flat and not only_dms,
        "sla_config": sla_config,
        "team_members": team_members,
        "social_accounts": social_accounts,
        "current_view": view,
        "active_filters": {
            "platform": platforms,
            "account": accounts,
            "type": types,
            "status": statuses,
            "assigned": assigned,
            "sentiment": sentiments,
            "date_from": date_from,
            "date_to": date_to,
            "range": date_range,
            "sort": sort,
            "q": q,
        },
    }

    if request.htmx:
        # The whole page, not the partial, when the browser's back/forward
        # button landed here. HTMX's history restore swaps the response into the
        # entire `<body>`, so the `_message_list` fragment alone would leave the
        # page torn apart — header, accounts column and split panel gone. Feeding
        # it the full page makes "back" re-render the feed it came from.
        if request.headers.get("HX-History-Restore-Request") == "true":
            return render(request, "inbox/feed.html", context)
        return render(request, "inbox/partials/_message_list.html", context)
    return render(request, "inbox/feed.html", context)


# --- Message Detail ---


@login_required
@require_permission("use_inbox")
def message_detail(request, workspace_id, message_id):
    """Message detail with thread, replies, notes, and reply composer.

    Narrowed to the channels this member was assigned, exactly as the feed is.
    A workspace filter alone was not enough: this is the deepest read in the app
    — full body, sender, every reply and every internal note — and an id outlives
    the assignment that produced it. Someone who worked a channel keeps ids in
    history and in assignment notifications, so revoking the channel hid it from
    their feed while this URL still answered in full.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to").filter(
            social_account__in=visible_accounts(request.workspace_membership)
        ),
        id=message_id,
        workspace=workspace,
    )

    # Mark as read → open
    if message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])

    context = _detail_context(workspace, message)

    if request.htmx:
        return render(request, "inbox/partials/_message_panel.html", context)
    return render(request, "inbox/message_detail.html", context)


# --- Reply ---


def _reply_error(request, message: str):
    """Render a send failure where the reply would have appeared.

    422 rather than 200 so HTMX doesn't swap this into the thread as if it were
    a delivered reply — the whole point is that nothing was sent.
    """
    return render(
        request,
        "inbox/partials/_reply_error.html",
        {"error_message": message},
        status=422,
    )


def _account_for_message(request, workspace_id, message_id, *args, **kwargs):
    """The channel a message belongs to, for the per-channel capability checks.

    Scoped to the workspace the URL names, so a message id from another tenant
    resolves to None and the capability check refuses — rather than fetching an
    account the caller was never entitled to see.
    """
    message = (
        InboxMessage.objects.filter(id=message_id, workspace_id=workspace_id).select_related("social_account").first()
    )
    return message.social_account if message else None


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it.

    Two gates, and both are needed: `reply_from_inbox` says the role may answer
    at all, `can_reply` says they may answer *as this channel*. Only the first
    was enforced, so the per-channel box saved a value nothing ever read.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Respuesta no válida.", status=400)

    body = form.cleaned_data["body"]
    account = message.social_account

    # Only record a reply that actually left the building. This used to swallow
    # every failure and store the reply anyway with an empty platform_reply_id,
    # so the thread showed it as sent while the platform never received it —
    # the operator had no way to tell. To note something without publishing it,
    # use an internal note (`add_note`) instead.
    try:
        from apps.publisher.engine import _resolve_publish_credentials

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        result = provider.reply_to_message(
            access_token=account.get_fresh_access_token(provider),
            message_id=message.platform_message_id,
            text=body,
            extra=message.extra,
        )
        platform_reply_id = result.platform_message_id
    except NotImplementedError:
        logger.info("Provider %s does not support reply_to_message.", account.platform)
        return _reply_error(request, f"{account.get_platform_display()} doesn't support replying from the inbox.")
    except (ConnectionError, TimeoutError, OSError) as exc:
        logger.exception("Network error sending reply for message %s: %s", message.id, exc)
        return _reply_error(
            request, "No se pudo contactar a la plataforma. Tu respuesta no se envió; inténtalo de nuevo."
        )
    except Exception:
        logger.exception("Failed to send reply for message %s", message.id)
        return _reply_error(request, "La plataforma rechazó la respuesta. No se envió.")

    InboxReply.objects.create(
        inbox_message=message,
        author=request.user,
        body=body,
        platform_reply_id=platform_reply_id,
    )

    # Auto-resolve on reply if configured
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        message.status = InboxMessage.Status.RESOLVED
        message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])

    # The whole panel, not the single bubble.
    #
    # Appending one item to the end of the thread put it in the wrong place as
    # soon as anything else had arrived since, and it bypassed the ordering that
    # _thread_turns does. Re-rendering costs one extra query and cannot get the
    # order wrong.
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message.

    Same two gates as `send_reply`. A note is not published, but it is written
    into a thread on a channel — and the panel it renders back carries that
    thread, so an ungated note was also a read of everything around it.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Nota no válida.", status=400)

    InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Moderation ---
#
# Two actions, deliberately named for what they do rather than lumped together
# as "remove". Hiding is reversible and the platform keeps the comment; deleting
# is permanent. Calling both "delete" is how someone reaches for the wrong one.
#
# In both cases our row survives. A hidden comment has to be restorable, and a
# deleted one still carries the thread we answered and who did what — dropping
# the record too would erase the evidence along with the comment.


def _moderation_provider(account):
    """A provider and a fresh token for one channel, or a reason it cannot be."""
    from apps.publisher.engine import _resolve_publish_credentials

    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    return provider, account.get_fresh_access_token(provider)


def _moderation_error(request, message_text):
    return render(
        request,
        "inbox/partials/_reply_error.html",
        {"error_message": message_text},
        status=422,
    )


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_moderate", _account_for_message)
@require_POST
def toggle_hidden(request, workspace_id, message_id):
    """Hide a comment from the public, or bring it back.

    On YouTube this is `setModerationStatus`, not deletion: the comment stops
    being publicly visible and the platform keeps it, which is why this is
    offered to whoever works the inbox rather than reserved for a manager.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    if message.deleted_on_platform:
        return _moderation_error(
            request, "Este comentario fue eliminado, por lo que ya no se puede ocultar ni restaurar."
        )
    if not message.is_moderatable:
        return _moderation_error(request, "Esto no es un comentario, así que no hay nada que ocultar en la plataforma.")

    account = message.social_account
    hide = not message.is_hidden

    try:
        provider, token = _moderation_provider(account)
        provider.set_comment_visibility(
            access_token=token,
            comment_id=message.platform_message_id,
            visible=not hide,
        )
    except NotImplementedError:
        return _moderation_error(request, f"{account.get_platform_display()} doesn't support hiding comments.")
    except RateLimitError:
        return _moderation_error(request, "La cuota de la plataforma está agotada por ahora. Inténtalo más tarde.")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Network error hiding comment %s", message.id)
        return _moderation_error(
            request, "No se pudo contactar a la plataforma. No se cambió nada; inténtalo de nuevo."
        )
    except APIError as exc:
        logger.exception("Platform refused the moderation change for %s: %s", message.id, exc)
        # The hardest failure to explain is unhiding a comment that had been
        # *rejected* rather than held for review. YouTube will not let an app
        # restore a rejected comment (it answers 400), while a held one comes
        # back fine — so say which of the two is going on instead of dumping
        # the API's cryptic reason.
        if not hide and message.platform == "youtube":
            return _moderation_error(
                request,
                "Este comentario no se puede restaurar desde la app. Si fue rechazado "
                "(y no solo retenido para revisión), YouTube no permite que las apps lo "
                "recuperen; restáuralo en youtube.com/comments, dentro de YouTube "
                "Studio.",
            )
        return _moderation_error(request, f"{account.get_platform_display()} lo rechazó: {exc}")

    # Written only after the platform agreed. Storing it first would show the
    # comment as hidden here while it stayed visible to everyone else, which is
    # the one thing this feature must never do.
    message.is_hidden = hide
    message.moderated_by = request.user
    message.save(update_fields=["is_hidden", "moderated_by"])

    return render(request, "inbox/partials/_message_panel.html", _detail_context(workspace, message))


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_delete_comments", _account_for_message)
@require_POST
def delete_comment(request, workspace_id, message_id):
    """Delete a comment permanently on the platform.

    Manager and above, because it cannot be undone. Note the platform limit
    This used to refuse anything that was not our own comment, on the belief
    that platforms only let you delete your own. That belief was wrong — a
    channel owner may delete comments on their own videos — and the refusal was
    unreachable anyway: the feed excludes outbound messages, so the panel could
    never be opened on one. The check hid the whole feature.

    So we try, and if the platform does refuse we say so and name the reversible
    alternative rather than leaving an API error nobody can act on.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    if message.deleted_on_platform:
        return _moderation_error(request, "Este comentario ya fue eliminado.")
    if not message.is_moderatable:
        return _moderation_error(
            request, "Esto no es un comentario, así que no hay nada que eliminar en la plataforma."
        )

    account = message.social_account

    try:
        provider, token = _moderation_provider(account)
        provider.delete_comment(access_token=token, comment_id=message.platform_message_id)
    except NotImplementedError:
        return _moderation_error(request, f"{account.get_platform_display()} doesn't support deleting comments.")
    except RateLimitError:
        return _moderation_error(request, "La cuota de la plataforma está agotada por ahora. Inténtalo más tarde.")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Network error deleting comment %s", message.id)
        return _moderation_error(
            request, "No se pudo contactar a la plataforma. No se eliminó nada; inténtalo de nuevo."
        )
    except APIError as exc:
        logger.exception("Platform refused the deletion of %s: %s", message.id, exc)
        return _moderation_error(
            request,
            f"{account.get_platform_display()} no permitió eliminarlo: {exc} "
            "Intenta ocultarlo: deja de ser visible al público y se puede deshacer.",
        )

    message.deleted_on_platform = timezone.now()
    message.moderated_by = request.user
    message.save(update_fields=["deleted_on_platform", "moderated_by"])

    return render(request, "inbox/partials/_message_panel.html", _detail_context(workspace, message))


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member.

    Gated on the channel because this is the one action that hands someone else
    a live message id: the notification below carries it. Without the gate, a
    member could route work on a channel they were never given.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Asignación no válida.", status=400)

    assigned_to_id = form.cleaned_data.get("assigned_to")
    if assigned_to_id:
        # Verify the user is a workspace member
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=assigned_to_id)
            .select_related("user")
            .first()
        )
        if not membership:
            return HttpResponse("El usuario no es miembro del espacio de trabajo.", status=400)
        # And they must hold this channel. Being in the workspace is not enough:
        # assigning work to someone who cannot open it produces a notification
        # that leads to a 403, which reads as a broken app rather than as a
        # permission boundary. The channel dialog is where access is granted.
        if not can_on_channel(membership, message.social_account, "can_reply"):
            return HttpResponse("Ese miembro no está asignado a este canal.", status=400)
        message.assigned_to = membership.user
    else:
        message.assigned_to = None

    message.save(update_fields=["assigned_to"])

    # Notify the assignee
    if message.assigned_to and message.assigned_to != request.user:
        notify(
            user=message.assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"Se te asignó un {message.get_message_type_display()}",
            body=f"De {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status.

    `bulk_action` refuses to archive a message on an unassigned channel; this is
    the same archive, one id at a time, and it used to allow it. Closing the bulk
    path and leaving the singular one open closes nothing.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Estado no válido.", status=400)

    message.status = form.cleaned_data["status"]
    message.save(update_fields=["status"])

    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually.

    Gated on `reply_from_inbox`, which is what editor, manager and owner hold
    and a plain viewer does not. Sentiment is not decoration: it is the record
    of how an audience talks to you, and it feeds the reporting — a viewer
    relabelling it quietly changes the numbers someone else reads.

    And gated on the channel for the same reason: the reporting it feeds is
    per-channel, so relabelling one you were never assigned edits numbers that
    belong to someone else's brand.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = SentimentForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Sentimiento no válido.", status=400)

    message.sentiment = form.cleaned_data["sentiment"]
    message.sentiment_source = InboxMessage.SentimentSource.MANUAL
    message.save(update_fields=["sentiment", "sentiment_source"])

    # The full panel, like change_status — the label appears in three places
    # (the header control, the badge on the message, and the dropdown's own
    # current value), so returning just one of them leaves the other two lying.
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Bulk Actions ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def bulk_action(request, workspace_id):
    """Perform bulk actions on multiple messages."""
    workspace = _get_workspace(request, workspace_id)

    form = BulkActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Acción masiva no válida.", status=400)

    message_ids = form.cleaned_data["message_ids"]
    action = form.cleaned_data["action"]
    value = form.cleaned_data.get("value", "")

    # Scoped to the channels this member may see, like the feed itself.
    #
    # The re-render below goes through `inbox_feed` precisely so the two lists
    # cannot drift — but the *write* was still a bare workspace filter, so a
    # crafted set of ids could archive or reassign messages on channels the
    # sender was never given. Reading was fixed and writing was not.
    qs = InboxMessage.objects.filter(
        id__in=message_ids,
        workspace=workspace,
        social_account__in=visible_accounts(
            request.workspace_membership,
            SocialAccount.objects.for_workspace(workspace.id),
        ),
    )

    if action == "mark_read":
        qs.filter(status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    elif action == "resolve":
        qs.exclude(status=InboxMessage.Status.ARCHIVED).update(status=InboxMessage.Status.RESOLVED)
    elif action == "archive":
        qs.update(status=InboxMessage.Status.ARCHIVED)
    elif action == "assign" and value:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user_id=value).first()
        if membership:
            # Only the messages the assignee can actually open, matching the
            # single-message path. A bulk selection spans channels, so assigning
            # the whole set to one person would otherwise hand them work on
            # channels they were never given — silently, and in bulk.
            qs.filter(social_account__in=visible_accounts(membership, capability="can_reply")).update(
                assigned_to=membership.user
            )

    # Re-render through inbox_feed rather than building a second list here.
    #
    # This used to be its own query, and it had drifted from the real one in
    # three ways at once: no parent/outbound filter, so replies and our own
    # words came back as separate items; no folder grouping, so the layout
    # changed under you after a bulk action; and no visible_accounts, which
    # meant a member briefly saw channels they were never assigned.
    #
    # Two places building "the inbox list" will always disagree eventually. Now
    # there is one, and this borrows it.
    #
    # The filters travel in the POST because a bulk action submits the form, not
    # the URL — without this the list would snap back to unfiltered and lose the
    # view the user was working in.
    request.GET = request.POST
    return inbox_feed(request, workspace_id)


# --- Saved Replies ---


@login_required
@require_permission("manage_workspace_settings")
def saved_replies_list(request, workspace_id):
    """List saved replies for a workspace."""
    workspace = _get_workspace(request, workspace_id)
    replies = SavedReply.objects.for_workspace(workspace.id)

    context = {"workspace": workspace, "saved_replies": replies}
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_create(request, workspace_id):
    """Create a new saved reply."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = SavedReplyForm(request.POST)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.workspace = workspace
            reply.created_by = request.user
            reply.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm()

    context = {"workspace": workspace, "form": form}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_edit(request, workspace_id, reply_id):
    """Edit an existing saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)

    if request.method == "POST":
        form = SavedReplyForm(request.POST, instance=reply)
        if form.is_valid():
            form.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm(instance=reply)

    context = {"workspace": workspace, "form": form, "saved_reply": reply}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def saved_reply_delete(request, workspace_id, reply_id):
    """Delete a saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)
    reply.delete()
    return redirect("inbox:saved_replies", workspace_id=workspace.id)


# --- SLA Config ---


@login_required
@require_permission("manage_workspace_settings")
def sla_config(request, workspace_id):
    """Configure SLA settings for inbox."""
    workspace = _get_workspace(request, workspace_id)
    config, _created = InboxSLAConfig.objects.get_or_create(
        workspace=workspace,
        defaults={"target_response_minutes": 120, "is_active": False},
    )

    if request.method == "POST":
        form = SLAConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return redirect("inbox:feed", workspace_id=workspace.id)
    else:
        form = SLAConfigForm(instance=config)

    context = {"workspace": workspace, "form": form, "config": config}
    return render(request, "inbox/sla_config.html", context)
