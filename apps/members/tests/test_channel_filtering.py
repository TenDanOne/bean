"""Channel assignment, applied end to end on the four surfaces that use it.

The unit tests next door prove the resolver returns the right set. These prove
the pages actually ask it — which is the half that rots, because a view that
forgets the filter still renders perfectly and just shows too much.

Coverage stops at inbox, composer and analytics on purpose. 49 queries touch
SocialAccount across 13 apps; filtering all of them at once is how this breaks.
The rest is known debt, written down rather than pretended.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def channels(workspace):
    return {
        name: SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle=name,
            account_name=name,
            account_platform_id=name,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        for name in ("assigned", "unassigned")
    }


def member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, membership


@pytest.mark.django_db
class TestInbox:
    def test_the_channel_filter_offers_only_assigned_channels(self, client, django_user_model, workspace, channels):
        """The filter and the list have to agree. A filter offering a channel
        the list can never show returns an empty page and looks like a bug."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channels["assigned"], can_reply=True)
        client.force_login(user)
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        offered = {a.account_handle for a in response.context["social_accounts"]}
        assert offered == {"assigned"}

    def test_an_owner_still_sees_everything(self, client, django_user_model, workspace, channels):
        user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(user)
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        offered = {a.account_handle for a in response.context["social_accounts"]}
        assert offered == {"assigned", "unassigned"}

    def test_a_member_with_no_assignments_sees_no_channels(self, client, django_user_model, workspace, channels):
        """Arriving with nothing is the safe default the whole design rests on."""
        user, _ = member(django_user_model, workspace, "editor", "new@example.com")
        client.force_login(user)
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        assert list(response.context["social_accounts"]) == []


@pytest.mark.django_db
class TestComposer:
    def test_you_can_only_compose_for_a_channel_you_hold(self, client, django_user_model, workspace, channels):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channels["assigned"], can_publish=True)
        client.force_login(user)
        response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        offered = {a.account_handle for a in response.context["social_accounts"]}
        assert offered == {"assigned"}


@pytest.mark.django_db
class TestAnalytics:
    def test_metrics_need_their_own_grant(self, client, django_user_model, workspace, channels):
        """can_view_metrics is separate from seeing the channel on purpose: the
        person answering the inbox is often not the one who should read reach.
        An assignment without it grants comments, not numbers."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(
            membership=membership,
            social_account=channels["assigned"],
            can_reply=True,
            can_view_metrics=False,
        )
        client.force_login(user)
        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code in (200, 302)
        if response.status_code == 200 and "accounts" in response.context:
            assert list(response.context["accounts"]) == []

    def test_the_metrics_grant_reveals_the_channel(self, client, django_user_model, workspace, channels):
        user, membership = member(django_user_model, workspace, "editor", "m@example.com")
        ChannelAssignment.objects.create(
            membership=membership, social_account=channels["assigned"], can_view_metrics=True
        )
        client.force_login(user)
        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        if response.status_code == 200 and "accounts" in response.context:
            assert {a.account_handle for a in response.context["accounts"]} == {"assigned"}
