"""Browsing the inbox by publication, and the titles that make it readable.

La bandeja se navega por publicación (ver test_inbox_ui.py). Aquí quedan los títulos y las acciones en lote.

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
        assert "tabs" in response.context

    def test_our_own_replies_stay_out_of_it(self, client, operator, workspace, channel):
        """The thing that was fixed once and came back through the side door."""
        target = comment(workspace, channel, "theirs", "vid-1", "1", title="One")
        ours = comment(workspace, channel, "ours", "vid-1", "2", title="One")
        ours.is_outbound = True
        ours.save(update_fields=["is_outbound"])

        client.force_login(operator)
        response = self._bulk(client, workspace, [str(target.id)], tab="com", only="all")
        listed = [m.body for folder in response.context["posts"] for m in folder["messages"]]
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
        response = self._bulk(client, workspace, [str(mine.id)], tab="com", only="all")
        labels = [f["label"] for f in response.context["posts"]]
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
