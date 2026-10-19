"""What a low-privilege member sees instead of a dead end.

Every empty state used to offer the same "Connect a channel" button, including
to people whose role cannot connect one — so the screen with nothing on it ended
at a bare 403 page with no sidebar to come back from. The analytics copy was
worse than useless: it said no accounts were connected to the workspace, which
was false. They are connected; they were not given to you.

The sidebar was leaking the other way, listing every channel in the workspace to
someone with no assignments at all, while the pages showed them nothing.
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
def channel(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle="the-channel",
        account_name="The Channel",
        account_platform_id="the-channel",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=workspace.organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=workspace.organization,
        defaults={"org_role": OrgMembership.OrgRole.MEMBER},
    )
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])
    return user, membership


@pytest.mark.django_db
class TestTheSidebarDoesNotLeak:
    def test_a_member_with_no_assignments_sees_no_channels(self, client, django_user_model, workspace, channel):
        """It listed every channel in the workspace — names, platforms and
        queued counts — while the inbox and analytics showed them nothing. The
        sidebar contradicted the page, and the sidebar was the one leaking."""
        user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))

        assert response.status_code == 200
        assert list(response.context["sidebar_channels"]) == []
        assert b"The Channel" not in response.content

    def test_an_assigned_channel_still_appears(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))

        assert [c.id for c in response.context["sidebar_channels"]] == [channel.id]

    def test_an_owner_sees_everything_without_assignments(self, client, django_user_model, workspace, channel):
        user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))

        assert [c.id for c in response.context["sidebar_channels"]] == [channel.id]


@pytest.mark.django_db
class TestNoDeadEnds:
    def test_the_connect_shortcuts_are_not_offered_to_those_who_cannot(
        self, client, django_user_model, workspace, channel
    ):
        """Every one of them points at social_accounts:connect, which requires
        manage_social_accounts."""
        user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        body = response.content.decode()

        connect_url = reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})
        assert connect_url not in body
        assert list(response.context["sidebar_connectable_platforms"]) == []

    def test_a_manager_still_gets_them(self, client, django_user_model, workspace, channel):
        user, _ = member(django_user_model, workspace, "manager", "m@example.com")
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        connect_url = reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})

        assert connect_url in response.content.decode()

    def test_the_channels_page_link_is_not_offered_either(self, client, django_user_model, workspace, channel):
        """An assigned channel is still listed — knowing which ones you work on
        is useful — but not as a link to a page that refuses them."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))
        body = response.content.decode()

        assert "The Channel" in body
        assert reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}) not in body


@pytest.mark.django_db
class TestTheEmptyStateExplainsItself:
    def test_the_inbox_names_who_to_ask(self, client, django_user_model, workspace, channel):
        member(django_user_model, workspace, "owner", "owner@example.com")
        user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(user)

        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}) + "?q=anything")
        body = response.content.decode()

        assert "No tienes canales asignados aquí" in body
        # "Ask an administrator" is useless in a five-person agency. display_name
        # falls back to the local part of the address.
        assert "Pídele acceso a <strong>owner</strong>" in body

    def test_analytics_does_not_claim_the_workspace_has_none(self, client, django_user_model, workspace, channel):
        """It used to say "no analytics-enabled accounts are connected to this
        workspace", which is a statement about the workspace and was false."""
        from apps.social_accounts.models import AnalyticsPlatformConfig

        AnalyticsPlatformConfig.objects.update_or_create(platform="youtube", defaults={"is_enabled": True})
        member(django_user_model, workspace, "owner", "owner@example.com")
        user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(user)

        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        body = response.content.decode()

        assert "están conectadas a este espacio" not in body
        assert "No hay canales con cifras para mostrar" in body
