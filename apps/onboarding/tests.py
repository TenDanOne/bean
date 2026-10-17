"""The get-started checklist survived the removal of the connection-link flow.

Both lived in this app, but they are unrelated features: the links were a magic
URL for outsiders to connect their own accounts, the checklist is a progress
widget for new members. Removing the app wholesale would have taken the useful
one with it, so these pin what remains.
"""

import pytest
from django.urls import reverse

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.onboarding.models import OnboardingChecklist
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Onboarding WS", organization=organization)


@pytest.fixture
def member(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.mark.django_db
class TestChecklist:
    def test_partial_renders(self, client, member, workspace):
        client.force_login(member)

        response = client.get(reverse("onboarding:checklist_partial", kwargs={"workspace_id": workspace.id}))

        assert response.status_code == 200

    def test_dismiss_records_the_choice(self, client, member, workspace):
        client.force_login(member)

        client.post(reverse("onboarding:dismiss_checklist", kwargs={"workspace_id": workspace.id}))

        checklist = OnboardingChecklist.objects.get(user=member, workspace=workspace)
        assert checklist.is_dismissed
        assert checklist.dismissed_at is not None

    def test_dismiss_is_idempotent(self, client, member, workspace):
        """update_or_create keyed on (user, workspace) — a second dismiss must
        not raise on the unique constraint."""
        client.force_login(member)
        url = reverse("onboarding:dismiss_checklist", kwargs={"workspace_id": workspace.id})

        client.post(url)
        client.post(url)

        assert OnboardingChecklist.objects.filter(user=member, workspace=workspace).count() == 1


@pytest.mark.django_db
def test_connection_link_routes_are_gone():
    """The public magic-link surface must not be reachable by name any more."""
    from django.urls import NoReverseMatch

    for name in ("connection_page", "connection_oauth_start", "oauth_callback"):
        with pytest.raises(NoReverseMatch):
            reverse(f"onboarding:{name}", kwargs={"token": "x"})
