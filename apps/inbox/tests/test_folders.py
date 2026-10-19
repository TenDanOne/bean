"""Browsing the inbox by publication, and the titles that make it readable.

The inbox used to be a flat list of remarks with no way to tell which video any
of them was about — answering meant opening the platform in another tab to find
out what the person was replying to.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.members.models import OrgMembership, WorkspaceMembership
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
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def operator(db, django_user_model, workspace):
    user = django_user_model.objects.create_user(
        email="op@example.com", password="pw12345!", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


def comment(workspace, channel, body, video_id, message_id, title=None):
    extra = {"video_id": video_id, "comment_id": message_id}
    if title:
        extra["post_title"] = title
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=channel,
        platform_message_id=message_id,
        message_type="comment",
        sender_name="Someone",
        body=body,
        extra=extra,
        received_at=timezone.now(),
    )


def feed(workspace):
    return reverse("inbox:feed", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestBrowsingFolders:
    def test_the_plain_inbox_lists_publications(self, client, operator, workspace, channel):
        comment(workspace, channel, "a", "vid-1", "1", title="Premed Video 01")
        comment(workspace, channel, "b", "vid-1", "2", title="Premed Video 01")
        comment(workspace, channel, "c", "vid-2", "3", title="Anatomy")
        client.force_login(operator)
        response = client.get(feed(workspace))
        assert response.status_code == 200
        labels = {f["label"]: len(f["messages"]) for f in response.context["folders"]}
        assert labels == {"Premed Video 01": 2, "Anatomy": 1}

    def test_opening_a_folder_shows_only_its_comments(self, client, operator, workspace, channel):
        comment(workspace, channel, "on one", "vid-1", "1", title="One")
        comment(workspace, channel, "on two", "vid-2", "2", title="Two")
        client.force_login(operator)
        response = client.get(feed(workspace), {"post": "vid-1"})
        bodies = [m.body for m in response.context["inbox_messages"]]
        assert bodies == ["on one"]
        assert response.context["active_folder"]["label"] == "One"

    def test_a_folder_links_out_to_the_platform(self, client, operator, workspace, channel):
        comment(workspace, channel, "a", "abc123", "1")
        client.force_login(operator)
        folder = client.get(feed(workspace)).context["folders"][0]
        assert folder["url"] == "https://www.youtube.com/watch?v=abc123"

    def test_a_folder_with_no_title_falls_back_to_its_id(self, client, operator, workspace, channel):
        """Worse than a title, but true — and still enough to tell two folders
        apart, which is the minimum a folder has to do."""
        comment(workspace, channel, "a", "abc123", "1")
        client.force_login(operator)
        assert client.get(feed(workspace)).context["folders"][0]["label"] == "abc123"


@pytest.mark.django_db
class TestSearchingIsNotBrowsing:
    def test_a_search_returns_comments_not_folders(self, client, operator, workspace, channel):
        """Someone typing "gracias" wants the comments that say it, not a list
        of videos that contain one."""
        comment(workspace, channel, "muchas gracias", "vid-1", "1", title="One")
        comment(workspace, channel, "nothing here", "vid-2", "2", title="Two")
        client.force_login(operator)
        response = client.get(feed(workspace), {"q": "gracias"})
        assert response.context["showing_folders"] is False
        assert [m.body for m in response.context["inbox_messages"]] == ["muchas gracias"]

    def test_narrowing_filters_keep_the_folders(self, client, operator, workspace, channel):
        """Platform, status and sentiment leave the publications intact, so the
        folders stay — and answer "which video has the most left to do".
        Flattening these was the first attempt and it was too blunt."""
        comment(workspace, channel, "a", "vid-1", "1", title="One")
        client.force_login(operator)
        response = client.get(feed(workspace), {"platform": "youtube"})
        assert response.context["showing_folders"] is True
        assert [f["label"] for f in response.context["folders"]] == ["One"]

    def test_the_queue_tabs_go_flat(self, client, operator, workspace, channel):
        """My Queue is a to-do list, not somewhere to browse."""
        comment(workspace, channel, "a", "vid-1", "1")
        client.force_login(operator)
        response = client.get(feed(workspace), {"view": "unassigned"})
        assert response.context["showing_folders"] is False

    def test_direct_messages_only_goes_flat(self, client, operator, workspace, channel):
        """A DM belongs to no publication; every one would pile into a single
        catch-all folder, which is a folder list of one."""
        client.force_login(operator)
        response = client.get(feed(workspace), {"type": "dm"})
        assert response.context["showing_folders"] is False

    def test_blank_filters_are_still_a_plain_browse(self, client, operator, workspace, channel):
        """`?platform=&status=` is what the filter bar submits when the selects
        sit on "All" — it is not a filter and must not switch modes."""
        comment(workspace, channel, "a", "vid-1", "1")
        client.force_login(operator)
        response = client.get(feed(workspace), {"platform": "", "status": "", "q": ""})
        assert response.context["showing_folders"] is True


@pytest.mark.django_db
class TestFoldersRespectChannelAssignment:
    def test_a_member_only_sees_folders_for_channels_they_hold(self, client, django_user_model, workspace, channel):
        """Comes for free from phase 3 — folders group the already-filtered
        queryset — but it is the kind of free that stops being free silently."""
        other = SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle="other",
            account_name="Other",
            account_platform_id="other",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        comment(workspace, channel, "mine", "vid-1", "1", title="Mine")
        comment(workspace, other, "theirs", "vid-2", "2", title="Theirs")

        from apps.members.models import ChannelAssignment

        user = django_user_model.objects.create_user(
            email="editor@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
        )
        membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="editor")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)

        client.force_login(user)
        response = client.get(feed(workspace))
        assert [f["label"] for f in response.context["folders"]] == ["Mine"]


class TestTitleFetching:
    def test_youtube_batches_fifty_at_a_time(self):
        """videos.list costs 1 quota unit per call and takes 50 ids, so a folder
        list is one unit rather than one per video."""
        from providers.youtube import YouTubeProvider

        calls = []

        class Fake(YouTubeProvider):
            def __init__(self):
                self.credentials = {}

            def _request(self, method, url, **kwargs):
                calls.append(kwargs["params"]["id"].split(","))

                class R:
                    @staticmethod
                    def json():
                        return {"items": []}

                return R()

        Fake().get_post_details("token", [f"v{i}" for i in range(120)])
        assert len(calls) == 3
        assert [len(c) for c in calls] == [50, 50, 20]

    def test_unresolvable_ids_are_simply_absent(self):
        """A deleted or private video has no entry, and the caller shows the id."""
        from providers.youtube import YouTubeProvider

        class Fake(YouTubeProvider):
            def __init__(self):
                self.credentials = {}

            def _request(self, method, url, **kwargs):
                class R:
                    @staticmethod
                    def json():
                        return {
                            "items": [
                                {
                                    "id": "alive",
                                    "snippet": {
                                        "title": "Still here",
                                        "publishedAt": "2026-01-15T10:00:00Z",
                                    },
                                }
                            ]
                        }

                return R()

        details = Fake().get_post_details("token", ["alive", "deleted"])
        assert details == {"alive": {"title": "Still here", "published_at": "2026-01-15T10:00:00Z"}}

    def test_the_publish_date_costs_no_extra_request(self):
        """part=snippet already returns publishedAt, so carrying it is free —
        the alternative was a second call for something shown beside every
        folder."""
        from providers.youtube import YouTubeProvider

        calls = []

        class Fake(YouTubeProvider):
            def __init__(self):
                self.credentials = {}

            def _request(self, method, url, **kwargs):
                calls.append(kwargs["params"])

                class R:
                    @staticmethod
                    def json():
                        return {"items": []}

                return R()

        Fake().get_post_details("token", ["v1"])
        assert len(calls) == 1
        assert calls[0]["part"] == "snippet"

    def test_a_platform_without_titles_says_so(self):
        """The optional-method pattern the rest of the provider base uses: a
        clear NotImplementedError, not a silent empty dict that looks like the
        video simply had no title."""
        from providers.bluesky import BlueskyProvider

        with pytest.raises(NotImplementedError, match="post details"):
            BlueskyProvider({}).get_post_details("token", ["a"])


@pytest.mark.django_db
class TestBulkActionsReturnTheSameInbox:
    """A bulk action used to hand back a list built by different rules.

    It re-queried on its own, without the parent/outbound filters, without the
    folder grouping and without the channel-visibility filter — so answering a
    few comments dropped you into a flat list containing your own replies and,
    worse, channels you had never been assigned.

    Now it renders through inbox_feed. These pin the three symptoms so a future
    shortcut cannot reintroduce them.
    """

    def _bulk(self, client, workspace, message_ids, **extra):
        return client.post(
            reverse("inbox:bulk_action", kwargs={"workspace_id": workspace.id}),
            {"message_ids": ",".join(message_ids), "action": "mark_read", **extra},
        )

    def test_the_list_comes_back_grouped(self, client, operator, workspace, channel):
        target = comment(workspace, channel, "a", "vid-1", "1", title="One")
        client.force_login(operator)
        response = self._bulk(client, workspace, [str(target.id)])
        assert response.status_code == 200
        assert response.context["showing_folders"] is True

    def test_our_own_replies_stay_out_of_it(self, client, operator, workspace, channel):
        """The thing that was fixed once and came back through the side door."""
        target = comment(workspace, channel, "theirs", "vid-1", "1", title="One")
        ours = comment(workspace, channel, "ours", "vid-1", "2", title="One")
        ours.is_outbound = True
        ours.save(update_fields=["is_outbound"])

        client.force_login(operator)
        response = self._bulk(client, workspace, [str(target.id)])
        listed = [m.body for folder in response.context["folders"] for m in folder["messages"]]
        assert listed == ["theirs"]

    def test_it_does_not_reveal_unassigned_channels(self, client, django_user_model, workspace, channel):
        """The permission leak: the second query never asked visible_accounts."""
        other = SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle="other",
            account_name="Other",
            account_platform_id="other",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        mine = comment(workspace, channel, "mine", "vid-1", "1", title="Mine")
        comment(workspace, other, "not mine", "vid-2", "2", title="Theirs")

        from apps.members.models import ChannelAssignment

        user = django_user_model.objects.create_user(
            email="ed@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
        )
        membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="editor")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)

        client.force_login(user)
        response = self._bulk(client, workspace, [str(mine.id)])
        labels = [f["label"] for f in response.context["folders"]]
        assert labels == ["Mine"]


@pytest.mark.django_db
class TestSentimentUpdate:
    """Changing sentiment must return the whole panel.

    It returned a lone badge into a target that replaces the entire panel, so
    relabelling a comment blanked the conversation and left a green pill on an
    empty page. The label appears in three places at once — the header control,
    the badge on the message, and the dropdown's own current value — so a
    partial response leaves two of them lying.
    """

    def test_the_conversation_survives_the_change(self, client, operator, workspace, channel):
        target = comment(workspace, channel, "hola", "vid-1", "1", title="One")
        client.force_login(operator)
        response = client.post(
            reverse(
                "inbox:change_sentiment",
                kwargs={"workspace_id": workspace.id, "message_id": target.id},
            ),
            {"sentiment": "positive"},
        )
        assert response.status_code == 200
        body = response.content.decode()
        # The message text proves the panel came back, not just its label.
        assert "hola" in body
        assert "Positivo" in body

    def test_a_manual_label_is_recorded_as_manual(self, client, operator, workspace, channel):
        """So the keyword guess never quietly overwrites a deliberate call."""
        target = comment(workspace, channel, "hola", "vid-1", "1")
        client.force_login(operator)
        client.post(
            reverse(
                "inbox:change_sentiment",
                kwargs={"workspace_id": workspace.id, "message_id": target.id},
            ),
            {"sentiment": "negative"},
        )
        target.refresh_from_db()
        assert target.sentiment == "negative"
        assert target.sentiment_source == InboxMessage.SentimentSource.MANUAL

    def test_a_viewer_cannot_relabel(self, client, django_user_model, workspace, channel):
        """Sentiment feeds the reporting; a viewer changing it quietly moves
        numbers someone else reads."""
        target = comment(workspace, channel, "hola", "vid-1", "1")
        user = django_user_model.objects.create_user(
            email="v@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
        )
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="viewer")
        client.force_login(user)
        response = client.post(
            reverse(
                "inbox:change_sentiment",
                kwargs={"workspace_id": workspace.id, "message_id": target.id},
            ),
            {"sentiment": "positive"},
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestDateAndSort:
    """Named windows and sort order, both keeping the publication folders.

    Narrowing by date leaves the publications intact, so folders stay — and a
    folder counting only what passed the filter is exactly what makes "which
    video still has old comments" answerable.
    """

    def _dated(self, workspace, channel, days_ago, message_id):
        from datetime import timedelta

        msg = comment(workspace, channel, f"c{message_id}", "vid-1", message_id, title="One")
        msg.received_at = timezone.now() - timedelta(days=days_ago)
        msg.save(update_fields=["received_at"])
        return msg

    def test_a_named_window_narrows_without_flattening(self, client, operator, workspace, channel):
        self._dated(workspace, channel, 2, "recent")
        self._dated(workspace, channel, 60, "old")
        client.force_login(operator)
        response = client.get(feed(workspace), {"range": "7d"})
        assert response.context["showing_folders"] is True
        bodies = [m.body for f in response.context["folders"] for m in f["messages"]]
        assert bodies == ["crecent"]

    def test_an_explicit_date_wins_over_a_preset(self, client, operator, workspace, channel):
        """Honouring the preset over a typed date would silently discard what
        the person actually asked for."""
        from datetime import timedelta

        self._dated(workspace, channel, 20, "midrange")
        client.force_login(operator)
        cutoff = (timezone.now() - timedelta(days=30)).date().isoformat()
        response = client.get(feed(workspace), {"range": "7d", "date_from": cutoff})
        bodies = [m.body for f in response.context["folders"] for m in f["messages"]]
        assert bodies == ["cmidrange"]

    def test_oldest_first_reorders_the_messages(self, client, operator, workspace, channel):
        self._dated(workspace, channel, 1, "newer")
        self._dated(workspace, channel, 40, "older")
        client.force_login(operator)
        response = client.get(feed(workspace), {"sort": "oldest", "q": "c"})
        assert [m.body for m in response.context["inbox_messages"]] == ["colder", "cnewer"]

    def test_oldest_first_ranks_folders_by_their_oldest_comment(self, client, operator, workspace, channel):
        """Sorting folders by their newest while claiming oldest-first would put
        a folder with one fresh comment above one waiting weeks."""
        from datetime import timedelta

        a = comment(workspace, channel, "old thread", "vid-old", "1", title="Old")
        a.received_at = timezone.now() - timedelta(days=40)
        a.save(update_fields=["received_at"])
        b = comment(workspace, channel, "fresh thread", "vid-new", "2", title="Fresh")
        b.received_at = timezone.now() - timedelta(days=1)
        b.save(update_fields=["received_at"])

        client.force_login(operator)
        response = client.get(feed(workspace), {"sort": "oldest"})
        assert [f["label"] for f in response.context["folders"]] == ["Old", "Fresh"]


@pytest.mark.django_db
class TestFiltersSurviveNavigation:
    """Opening a folder used to drop every active filter.

    The link was a bare `?post=<id>`, so "last 3 months" narrowed the folder
    list and then the folder itself showed comments from years back. The date
    filter looked broken when it was the navigation losing it.
    """

    def test_the_folder_link_carries_the_active_filters(self, client, operator, workspace, channel):
        comment(workspace, channel, "a", "vid-1", "1", title="One")
        client.force_login(operator)
        response = client.get(feed(workspace), {"range": "90d", "sort": "oldest"})
        assert "range=90d" in response.context["filter_qs"]
        assert "sort=oldest" in response.context["filter_qs"]

    def test_the_folder_itself_is_still_filtered(self, client, operator, workspace, channel):
        """The symptom that was reported: a February comment inside a folder
        while the window said last three months."""
        from datetime import timedelta

        old = comment(workspace, channel, "february", "vid-1", "1", title="One")
        old.received_at = timezone.now() - timedelta(days=170)
        old.save(update_fields=["received_at"])
        recent = comment(workspace, channel, "recent", "vid-1", "2", title="One")
        recent.received_at = timezone.now() - timedelta(days=3)
        recent.save(update_fields=["received_at"])

        client.force_login(operator)
        response = client.get(feed(workspace), {"post": "vid-1", "range": "90d"})
        assert [m.body for m in response.context["inbox_messages"]] == ["recent"]

    def test_the_folder_query_string_drops_blanks_and_post(self, client, operator, workspace, channel):
        """The filter bar submits `?platform=&status=` when its selects sit on
        "All"; carrying those would make every link noisier for nothing."""
        comment(workspace, channel, "a", "vid-1", "1")
        client.force_login(operator)
        response = client.get(feed(workspace), {"platform": "", "range": "7d", "post": ""})
        qs = response.context["filter_qs"]
        assert "platform=" not in qs
        assert "post=" not in qs
        assert "range=7d" in qs
