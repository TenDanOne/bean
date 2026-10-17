"""Channel scoping on the single-message inbox routes.

The feed and `bulk_action` were narrowed to assigned channels; the by-id routes
next to them were not. That asymmetry is the interesting part, so these tests
are written against the singular endpoints specifically — a message id is easy
to keep (browser history, an assignment notification's payload) and it outlives
the assignment that produced it.

`test_moderation.py` covers the same boundary for hiding and deleting. This file
covers reading a thread, noting on it, assigning it, and relabelling it.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage, InternalNote
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


def make_channel(workspace, handle):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle=handle,
        account_name=handle.title(),
        account_platform_id=handle,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def channel_a(workspace):
    return make_channel(workspace, "alpha")


@pytest.fixture
def channel_b(workspace):
    """The channel nobody in these tests is assigned to."""
    return make_channel(workspace, "beta")


def member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=workspace.organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=workspace.organization,
        defaults={"org_role": OrgMembership.OrgRole.MEMBER},
    )
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, membership


def comment(workspace, channel, *, platform_id="c1", body="hello"):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=channel,
        platform_message_id=platform_id,
        message_type="comment",
        sender_name="Someone",
        body=body,
        received_at=timezone.now(),
    )


def url(name, workspace, message):
    return reverse(f"inbox:{name}", kwargs={"workspace_id": workspace.id, "message_id": message.id})


# The four writes that took a bare workspace filter, with the POST body each
# needs to be otherwise valid — so a refusal is the channel gate talking and not
# a form error.
WRITE_ROUTES = [
    ("add_note", {"body": "internal"}),
    ("change_status", {"status": InboxMessage.Status.ARCHIVED}),
    ("change_sentiment", {"sentiment": InboxMessage.Sentiment.NEGATIVE}),
]


@pytest.mark.django_db
class TestReadingAThread:
    def test_an_assigned_channel_opens(self, client, django_user_model, workspace, channel_a):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_a)
        client.force_login(user)

        response = client.get(url("message_detail", workspace, message))

        assert response.status_code == 200

    def test_an_unassigned_channel_is_a_404(self, client, django_user_model, workspace, channel_a, channel_b):
        """The id is real and in their workspace; the channel was never theirs."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_b)
        client.force_login(user)

        response = client.get(url("message_detail", workspace, message))

        assert response.status_code == 404

    def test_revoking_the_assignment_closes_the_thread(self, client, django_user_model, workspace, channel_a):
        """The case the feed filter alone did not cover.

        They worked this channel, so the id is in their history. Taking the
        channel away has to take the thread with it.
        """
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        assignment = ChannelAssignment.objects.create(
            membership=membership, social_account=channel_a, can_reply=True
        )
        message = comment(workspace, channel_a)
        client.force_login(user)
        assert client.get(url("message_detail", workspace, message)).status_code == 200

        assignment.delete()

        assert client.get(url("message_detail", workspace, message)).status_code == 404

    def test_an_owner_needs_no_assignment(self, client, django_user_model, workspace, channel_b):
        """Owner is the one unrestricted role — see channels.UNRESTRICTED_ROLES."""
        user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        message = comment(workspace, channel_b)
        client.force_login(user)

        response = client.get(url("message_detail", workspace, message))

        assert response.status_code == 200


@pytest.mark.django_db
class TestWritingToAThread:
    @pytest.mark.parametrize("route,payload", WRITE_ROUTES)
    def test_an_assigned_channel_is_allowed(
        self, client, django_user_model, workspace, channel_a, route, payload
    ):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_a)
        client.force_login(user)

        response = client.post(url(route, workspace, message), payload)

        assert response.status_code == 200

    @pytest.mark.parametrize("route,payload", WRITE_ROUTES)
    def test_an_unassigned_channel_is_refused(
        self, client, django_user_model, workspace, channel_a, channel_b, route, payload
    ):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_b)
        client.force_login(user)

        response = client.post(url(route, workspace, message), payload)

        assert response.status_code == 403

    def test_archiving_one_id_matches_what_bulk_refuses(
        self, client, django_user_model, workspace, channel_a, channel_b
    ):
        """The concrete asymmetry this closed: bulk refused, singular allowed."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_b)
        client.force_login(user)

        client.post(url("change_status", workspace, message), {"status": InboxMessage.Status.ARCHIVED})

        message.refresh_from_db()
        assert message.status != InboxMessage.Status.ARCHIVED

    def test_a_refused_note_is_not_written(self, client, django_user_model, workspace, channel_a, channel_b):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        message = comment(workspace, channel_b)
        client.force_login(user)

        client.post(url("add_note", workspace, message), {"body": "internal"})

        assert not InternalNote.objects.filter(inbox_message=message).exists()


@pytest.mark.django_db
class TestAssignment:
    def test_assigning_on_an_unassigned_channel_is_refused(
        self, client, django_user_model, workspace, channel_a, channel_b
    ):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        other, other_membership = member(django_user_model, workspace, "editor", "x@example.com")
        ChannelAssignment.objects.create(membership=other_membership, social_account=channel_b, can_reply=True)
        message = comment(workspace, channel_b)
        client.force_login(user)

        response = client.post(url("assign", workspace, message), {"assigned_to": str(other.id)})

        assert response.status_code == 403
        message.refresh_from_db()
        assert message.assigned_to is None

    def test_the_assignee_must_hold_the_channel(self, client, django_user_model, workspace, channel_a):
        """Being in the workspace is not enough.

        The notification carries the message id, so assigning to someone who
        cannot open it hands them a link to a 403.
        """
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        stranger, _ = member(django_user_model, workspace, "editor", "s@example.com")
        message = comment(workspace, channel_a)
        client.force_login(user)

        response = client.post(url("assign", workspace, message), {"assigned_to": str(stranger.id)})

        assert response.status_code == 400
        message.refresh_from_db()
        assert message.assigned_to is None

    def test_assigning_to_a_holder_of_the_channel_works(self, client, django_user_model, workspace, channel_a):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel_a, can_reply=True)
        colleague, colleague_membership = member(django_user_model, workspace, "editor", "c@example.com")
        ChannelAssignment.objects.create(
            membership=colleague_membership, social_account=channel_a, can_reply=True
        )
        message = comment(workspace, channel_a)
        client.force_login(user)

        response = client.post(url("assign", workspace, message), {"assigned_to": str(colleague.id)})

        assert response.status_code == 200
        message.refresh_from_db()
        assert message.assigned_to == colleague


@pytest.mark.django_db
class TestBulkAssignment:
    def test_bulk_assign_skips_channels_the_assignee_lacks(
        self, client, django_user_model, workspace, channel_a, channel_b
    ):
        """A bulk selection spans channels; the assignee may not hold them all."""
        owner, _ = member(django_user_model, workspace, "owner", "o@example.com")
        colleague, colleague_membership = member(django_user_model, workspace, "editor", "c@example.com")
        ChannelAssignment.objects.create(
            membership=colleague_membership, social_account=channel_a, can_reply=True
        )
        on_a = comment(workspace, channel_a, platform_id="a1")
        on_b = comment(workspace, channel_b, platform_id="b1")
        client.force_login(owner)

        client.post(
            reverse("inbox:bulk_action", kwargs={"workspace_id": workspace.id}),
            {"message_ids": [str(on_a.id), str(on_b.id)], "action": "assign", "value": str(colleague.id)},
        )

        on_a.refresh_from_db()
        on_b.refresh_from_db()
        assert on_a.assigned_to == colleague
        assert on_b.assigned_to is None
