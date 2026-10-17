"""A manager runs their brands, including who does what on them.

The service layer already implemented this rule — strictly below yourself, in
every brand the person works on — and nothing could reach it: the views required
an *organization* admin, which a workspace manager is not, and the template hid
the whole actions menu behind the same org-only flag. So the rule was written,
tested, and unreachable.

The other half is what a manager may **not** touch: the owner. They rank above,
and they are the one handing out channel access in the first place — a
subordinate editing that would invert the arrangement.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def other_workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand B")


def member(django_user_model, workspace, role, email, *, org_role=OrgMembership.OrgRole.MEMBER):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=workspace.organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=workspace.organization,
        defaults={"org_role": org_role},
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


def org_membership_of(user, workspace):
    return OrgMembership.objects.get(user=user, organization=workspace.organization)


def role_url(org_membership):
    return reverse("members:update_role", kwargs={"membership_id": org_membership.id})


def role_of(user, workspace):
    return WorkspaceMembership.objects.get(user=user, workspace=workspace).workspace_role


@pytest.mark.django_db
class TestAManagerCanChangeRolesBelowThem:
    def test_a_manager_makes_a_viewer_an_editor(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(target, workspace)),
            {"workspace_role": "editor"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert role_of(target, workspace) == "editor"

    def test_the_picker_offers_only_what_they_may_grant(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        offered = dict(response.context["workspace_role_choices"])

        assert set(offered) == {"editor", "reviewer", "viewer"}

    def test_the_actions_menu_is_there_for_someone_below(self, client, django_user_model, workspace):
        """Asserted on the flag the template branches on. Hiding this behind an
        org-only `is_admin` is what made every other fix invisible."""
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        flags = {e["user"].id: e["can_manage"] for e in response.context["members_data"]}

        assert flags[target.id] is True
        assert flags[manager.id] is False


@pytest.mark.django_db
class TestAManagerCannotTouchTheOwner:
    def test_no_actions_menu_on_the_owner_row(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        owner = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        flags = {e["user"].id: e["can_manage"] for e in response.context["members_data"]}

        assert flags[owner.id] is False

    def test_posting_the_owners_role_anyway_is_refused(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        owner = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(owner, workspace)),
            {"workspace_role": "viewer"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 403
        assert role_of(owner, workspace) == "owner"

    def test_no_channel_button_on_the_owner_row(self, client, django_user_model, workspace):
        """The owner connects the channels and hands them out. A manager being
        offered the dialog on that row — which then 403s — is the arrangement
        upside down.

        This is the flag the badge branches on. It used to be computed from
        "may I assign in this brand?" alone, with no comparison of the two
        roles, despite a comment claiming otherwise.
        """
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        owner = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        flags = {
            e["user"].id: [wm.can_manage_channels for wm in e["workspace_memberships"]]
            for e in response.context["members_data"]
        }

        assert flags[owner.id] == [False]

    def test_but_the_button_is_there_for_someone_below(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        flags = {
            e["user"].id: [wm.can_manage_channels for wm in e["workspace_memberships"]]
            for e in response.context["members_data"]
        }

        assert flags[target.id] == [True]

    def test_a_peer_manager_is_refused_too(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m1@example.com")
        peer = member(django_user_model, workspace, "manager", "m2@example.com")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(peer, workspace)),
            {"workspace_role": "viewer"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 403
        assert role_of(peer, workspace) == "manager"

    def test_they_cannot_promote_anyone_to_manager(self, client, django_user_model, workspace):
        """Two managers able to promote each other is a loop with nobody above."""
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(target, workspace)),
            {"workspace_role": "manager"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 422
        assert role_of(target, workspace) == "editor"


@pytest.mark.django_db
class TestAuthorityIsPerBrand:
    def test_someone_who_also_works_on_a_brand_they_do_not_manage_is_out_of_reach(
        self, client, django_user_model, workspace, other_workspace
    ):
        """The role applies everywhere the person works, so changing it reaches
        into a brand this manager has no say over. Refusing is the honest
        answer; a partial change would leave them a different role per brand,
        which is exactly what the single-role model exists to prevent.
        """
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        WorkspaceMembership.objects.create(user=target, workspace=other_workspace, workspace_role="viewer")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(target, workspace)),
            {"workspace_role": "editor"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 403
        assert role_of(target, workspace) == "viewer"

    def test_managing_both_brands_is_enough(self, client, django_user_model, workspace, other_workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        WorkspaceMembership.objects.create(user=manager, workspace=other_workspace, workspace_role="manager")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        WorkspaceMembership.objects.create(user=target, workspace=other_workspace, workspace_role="viewer")
        client.force_login(manager)

        response = client.post(
            role_url(org_membership_of(target, workspace)),
            {"workspace_role": "editor"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert role_of(target, workspace) == "editor"
        assert role_of(target, other_workspace) == "editor"


@pytest.mark.django_db
class TestRemovingFromTheAccountStaysWithAdmins:
    def test_a_manager_is_not_offered_the_remove_button(self, client, django_user_model, workspace):
        """Taking someone out of the account is not managing a brand. To get
        them off their brand a manager unticks it instead."""
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(manager)

        response = client.get(reverse("members:list"))
        flags = {e["user"].id: e["can_remove"] for e in response.context["members_data"]}

        assert flags[target.id] is False

    def test_and_the_endpoint_refuses_them(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        target = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(manager)

        response = client.post(
            reverse("members:remove", kwargs={"membership_id": org_membership_of(target, workspace).id})
        )

        assert response.status_code == 403
        assert OrgMembership.objects.filter(user=target, organization=workspace.organization).exists()
