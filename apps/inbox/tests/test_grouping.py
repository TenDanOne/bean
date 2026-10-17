"""Linking comments to the post they were left on, and grouping by it.

`related_post` was declared long before anything wrote to it — filled on zero
rows — so the inbox was a flat list of remarks with no way to tell what any of
them was about. Answering meant opening the platform in another tab.
"""

import pytest
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.inbox.grouping import (
    extract_platform_post_id,
    group_by_post,
    resolve_related_post,
)
from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def channel(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle="chan",
        account_name="Chan",
        account_platform_id="chan",
    )


def make_post(workspace, channel, user, platform_post_id, caption="A video about soup"):
    post = Post.objects.create(workspace=workspace, author=user, caption=caption)
    return PlatformPost.objects.create(post=post, social_account=channel, platform_post_id=platform_post_id)


def make_comment(workspace, channel, text, extra, received=None, status=None):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=channel,
        platform_message_id=extra.get("comment_id", text),
        message_type="comment",
        sender_name="Someone",
        body=text,
        extra=extra,
        status=status or InboxMessage.Status.UNREAD,
        received_at=received or timezone.now(),
    )


class TestExtractingTheId:
    def test_reads_the_key_each_provider_uses(self):
        """YouTube and Facebook say video_id, Pinterest says media_id. One
        resolver, so a new platform is a line in POST_ID_KEYS rather than a
        branch in the inbox."""

        class Fake:
            def __init__(self, extra):
                self.extra = extra

        assert extract_platform_post_id(Fake({"video_id": "abc"})) == "abc"
        assert extract_platform_post_id(Fake({"media_id": 123})) == "123"
        assert extract_platform_post_id(Fake({})) is None
        assert extract_platform_post_id(Fake(None)) is None


@pytest.mark.django_db
class TestResolving:
    def test_a_comment_finds_the_post_it_was_left_on(self, workspace, channel, user):
        platform_post = make_post(workspace, channel, user, "vid-1")
        comment = make_comment(workspace, channel, "nice", {"video_id": "vid-1"})
        assert resolve_related_post(comment) == platform_post

    def test_a_comment_on_a_post_published_elsewhere_matches_nothing(self, workspace, channel, user):
        """Not a bug: a video uploaded directly on YouTube has no PlatformPost
        to point at, and saying so is more honest than guessing."""
        make_post(workspace, channel, user, "vid-1")
        comment = make_comment(workspace, channel, "hm", {"video_id": "uploaded-elsewhere"})
        assert resolve_related_post(comment) is None

    def test_the_match_cannot_cross_channels(self, workspace, organization, user):
        """Platform ids are unique per platform, not globally — matching on the
        id alone could attach a comment to another channel's post."""
        one = SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle="a",
            account_name="A",
            account_platform_id="a",
        )
        two = SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle="b",
            account_name="B",
            account_platform_id="b",
        )
        make_post(workspace, one, user, "shared-id")
        comment_on_two = make_comment(workspace, two, "hi", {"video_id": "shared-id"})
        assert resolve_related_post(comment_on_two) is None

    def test_the_cache_stops_one_query_per_comment(self, workspace, channel, user, django_assert_num_queries):
        """A sync pulls hundreds of comments belonging to a handful of posts."""
        make_post(workspace, channel, user, "vid-1")
        comments = [
            make_comment(workspace, channel, f"c{i}", {"video_id": "vid-1", "comment_id": f"c{i}"}) for i in range(5)
        ]
        cache: dict = {}
        with django_assert_num_queries(1):
            for comment in comments:
                resolve_related_post(comment, cache)


@pytest.mark.django_db
class TestGrouping:
    def test_comments_gather_under_their_publication(self, workspace, channel, user):
        first = make_post(workspace, channel, user, "vid-1", caption="Soup")
        second = make_post(workspace, channel, user, "vid-2", caption="Bread")
        a = make_comment(workspace, channel, "on soup", {"video_id": "vid-1", "comment_id": "1"})
        b = make_comment(workspace, channel, "also soup", {"video_id": "vid-1", "comment_id": "2"})
        c = make_comment(workspace, channel, "on bread", {"video_id": "vid-2", "comment_id": "3"})
        for message in (a, b, c):
            message.related_post = resolve_related_post(message)
            message.save(update_fields=["related_post"])

        groups = group_by_post([a, b, c])
        by_key = {g["key"]: len(g["messages"]) for g in groups}
        assert by_key["vid-1"] == 2
        assert by_key["vid-2"] == 1
        # related_post still enriches the folder even though it no longer keys it.
        assert {g["post"] for g in groups} == {first, second}

    def test_comments_on_a_video_we_never_published_still_get_a_folder(self, workspace, channel, user):
        """The case that matters most in practice: a channel connected to an
        account with years of existing videos has no PlatformPost for any of
        them. Keying on related_post would drop every one of those comments into
        a single undifferentiated pile."""
        a = make_comment(workspace, channel, "on the old video", {"video_id": "old-1", "comment_id": "1"})
        b = make_comment(workspace, channel, "same video", {"video_id": "old-1", "comment_id": "2"})
        c = make_comment(workspace, channel, "different one", {"video_id": "old-2", "comment_id": "3"})
        groups = group_by_post([a, b, c])
        assert len(groups) == 2
        assert all(g["post"] is None for g in groups)
        assert {g["key"] for g in groups} == {"old-1", "old-2"}

    def test_a_folder_without_a_title_is_labelled_by_its_id(self, workspace, channel, user):
        """A poor label, but an honest one — and still enough to tell two
        folders apart, which is the minimum a folder has to do."""
        comment = make_comment(workspace, channel, "hi", {"video_id": "abc123", "comment_id": "1"})
        assert group_by_post([comment])[0]["label"] == "abc123"

    def test_a_stored_title_beats_the_raw_id(self, workspace, channel, user):
        comment = make_comment(
            workspace,
            channel,
            "hi",
            {"video_id": "abc123", "comment_id": "1", "post_title": "Premed Video 01"},
        )
        assert group_by_post([comment])[0]["label"] == "Premed Video 01"

    def test_each_folder_links_to_the_publication(self, workspace, channel, user):
        """Free, built from the id we already hold, and it is what makes a
        folder usable when we have no title."""
        comment = make_comment(workspace, channel, "hi", {"video_id": "abc123", "comment_id": "1"})
        assert group_by_post([comment])[0]["url"] == "https://www.youtube.com/watch?v=abc123"

    def test_messages_with_no_post_id_at_all_sort_last(self, workspace, channel, user):
        """Direct messages and anything the provider gave us no post id for.
        They still need answering, so they get a bucket rather than vanishing."""
        withid = make_comment(workspace, channel, "real", {"video_id": "vid-1", "comment_id": "1"})
        loose = make_comment(workspace, channel, "a DM", {"comment_id": "2"})
        groups = group_by_post([loose, withid])
        assert groups[-1]["platform_post_id"] is None
        assert groups[-1]["messages"] == [loose]

    def test_each_group_counts_its_own_unread(self, workspace, channel, user):
        make_post(workspace, channel, user, "vid-1")
        read = make_comment(
            workspace, channel, "seen", {"video_id": "vid-1", "comment_id": "1"}, status=InboxMessage.Status.RESOLVED
        )
        unread = make_comment(workspace, channel, "new", {"video_id": "vid-1", "comment_id": "2"})
        for message in (read, unread):
            message.related_post = resolve_related_post(message)
            message.save(update_fields=["related_post"])
        groups = group_by_post([read, unread])
        assert groups[0]["unread"] == 1

    def test_a_long_caption_is_trimmed_for_the_folder_label(self, workspace, channel, user):
        make_post(workspace, channel, user, "vid-1", caption="x" * 200)
        comment = make_comment(workspace, channel, "hi", {"video_id": "vid-1", "comment_id": "1"})
        comment.related_post = resolve_related_post(comment)
        comment.save(update_fields=["related_post"])
        label = group_by_post([comment])[0]["label"]
        assert len(label) <= 71
        assert label.endswith("…")
