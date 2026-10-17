"""What roles may be granted, and which ones may not be granted at all.

Two rules live here and they fail in opposite directions.

`reviewer` was added to the model and to the picker but never to the copy of the
hierarchy that `services` validated against, so the interface offered a role the
server then rejected — a grant that *should* work and did not.

Workspace `owner` is the reverse: it was offered and accepted, and a workspace
owner sees every channel with no assignment at all, so handing it out undoes the
whole channel-access layer in one click.
"""

import pytest
from django.utils import timezone

from apps.members.models import (
    OrgMembership,
    WorkspaceMembership,
    assignable_workspace_role_choices,
)
from apps.members.services import set_member_role, update_workspace_assignments
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def owner(db, organization, django_user_model):
    user = django_user_model.objects.create_user(
        email="owner@example.com", password="pw12345!", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=organization,
        defaults={"org_role": OrgMembership.OrgRole.OWNER},
    )
    return user


@pytest.fixture
def teammate(db, django_user_model):
    user = django_user_model.objects.create_user(
        email="team@example.com", password="pw12345!", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.filter(user=user).delete()
    return user


def assign(organization, user, workspace, role, inviter):
    return update_workspace_assignments(
        organization,
        user,
        [{"workspace_id": str(workspace.id), "role": role}],
        inviter=inviter,
    )


@pytest.mark.django_db
class TestReviewerIsGrantable:
    def test_reviewer_can_be_assigned(self, organization, owner, teammate, workspace):
        """Regression. This raised "Unknown workspace role: 'reviewer'".

        `services` kept its own copy of the hierarchy and never received the role
        when it was added. An unrecognised role scores 0, and 0 is rejected — so
        the one role built for outside reviewers was the one role that could not
        be granted.
        """
        assign(organization, teammate, workspace, "reviewer", owner)
        membership = WorkspaceMembership.objects.get(user=teammate, workspace=workspace)
        assert membership.workspace_role == "reviewer"

    def test_every_offered_role_can_actually_be_granted(self, organization, owner, teammate, workspace):
        """The picker and the server must not disagree about what exists.

        Written against the choices rather than a hardcoded list so that adding
        a role without teaching the hierarchy about it fails here instead of in
        someone's face.
        """
        for value, _label in assignable_workspace_role_choices():
            assign(organization, teammate, workspace, value, owner)
            membership = WorkspaceMembership.objects.get(user=teammate, workspace=workspace)
            assert membership.workspace_role == value


@pytest.mark.django_db
class TestGrantingIsBoundedByYourOwnRole:
    """You hand out what is strictly below you, never your own tier.

    A manager offering another manager is a loop with nobody above it: the two
    could promote each other, and demote whoever tried to stop them.
    """

    def test_an_owner_offers_manager_and_down(self):
        offered = dict(assignable_workspace_role_choices("owner"))
        assert set(offered) == {"manager", "editor", "reviewer", "viewer"}

    def test_a_manager_offers_editor_and_down(self):
        offered = dict(assignable_workspace_role_choices("manager"))
        assert set(offered) == {"editor", "reviewer", "viewer"}

    def test_an_editor_offers_nothing_they_could_use(self):
        offered = dict(assignable_workspace_role_choices("editor"))
        assert "manager" not in offered
        assert "editor" not in offered

    def test_a_manager_cannot_make_another_manager(self, organization, teammate, workspace, django_user_model):
        manager = django_user_model.objects.create_user(
            email="manager@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=manager).exclude(organization=organization).delete()
        OrgMembership.objects.update_or_create(
            user=manager, organization=organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
        )
        WorkspaceMembership.objects.create(user=manager, workspace=workspace, workspace_role="manager")
        WorkspaceMembership.objects.create(user=teammate, workspace=workspace, workspace_role="viewer")

        with pytest.raises(ValueError, match="below your own"):
            set_member_role(organization, teammate, "manager", caller=manager)

        assert WorkspaceMembership.objects.get(user=teammate, workspace=workspace).workspace_role == "viewer"

    def test_a_manager_can_make_an_editor(self, organization, teammate, workspace, django_user_model):
        manager = django_user_model.objects.create_user(
            email="manager2@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=manager).exclude(organization=organization).delete()
        OrgMembership.objects.update_or_create(
            user=manager, organization=organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
        )
        WorkspaceMembership.objects.create(user=manager, workspace=workspace, workspace_role="manager")
        WorkspaceMembership.objects.create(user=teammate, workspace=workspace, workspace_role="viewer")

        set_member_role(organization, teammate, "editor", caller=manager)

        assert WorkspaceMembership.objects.get(user=teammate, workspace=workspace).workspace_role == "editor"

    def test_a_manager_cannot_demote_a_peer(self, organization, teammate, workspace, django_user_model):
        """The other half of the loop: blocking the promotion is useless if the
        demotion still works."""
        manager = django_user_model.objects.create_user(
            email="manager3@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=manager).exclude(organization=organization).delete()
        OrgMembership.objects.update_or_create(
            user=manager, organization=organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
        )
        WorkspaceMembership.objects.create(user=manager, workspace=workspace, workspace_role="manager")
        WorkspaceMembership.objects.create(user=teammate, workspace=workspace, workspace_role="manager")

        with pytest.raises(ValueError, match="at or above your own"):
            set_member_role(organization, teammate, "viewer", caller=manager)


@pytest.mark.django_db
class TestOwnershipIsNotGranted:
    def test_owner_is_not_offered_in_the_picker(self):
        assert "owner" not in dict(assignable_workspace_role_choices())

    def test_granting_owner_is_refused_by_the_server(self, organization, owner, teammate, workspace):
        """The picker not offering it is convenience; this is the boundary."""
        with pytest.raises(ValueError, match="Transfer ownership instead"):
            assign(organization, teammate, workspace, "owner", owner)

    def test_an_existing_owner_survives_a_resave(self, organization, owner, teammate, workspace):
        """Refusing the value outright would mean opening an existing owner's
        form and pressing save silently demoted them — a destructive edit
        dressed up as a security check.
        """
        WorkspaceMembership.objects.create(user=teammate, workspace=workspace, workspace_role="owner")
        assign(organization, teammate, workspace, "owner", owner)
        membership = WorkspaceMembership.objects.get(user=teammate, workspace=workspace)
        assert membership.workspace_role == "owner"

    def test_promoting_an_existing_member_to_owner_is_refused(self, organization, owner, teammate, workspace):
        """The case the exemption above must not open: they hold something
        else, so this is a promotion, not a resave."""
        WorkspaceMembership.objects.create(user=teammate, workspace=workspace, workspace_role="editor")
        with pytest.raises(ValueError, match="Transfer ownership instead"):
            assign(organization, teammate, workspace, "owner", owner)
        membership = WorkspaceMembership.objects.get(user=teammate, workspace=workspace)
        assert membership.workspace_role == "editor"
