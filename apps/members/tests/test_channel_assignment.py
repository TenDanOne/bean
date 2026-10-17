"""Channel assignment: who sees which channel, and who may do what on it.

The rule worth pinning is the one that is not obvious from reading a queryset:
**only an owner sees every channel without an assignment.** Manager included.
That is what makes adding a member safe by default — they arrive seeing
nothing — and what lets a personal-account owner be a reviewer with one channel
and see nothing else in the workspace.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from apps.members.channels import (
    assignable_accounts,
    can_assign_channels,
    can_assign_to,
    can_on_channel,
    sees_all_channels,
    visible_accounts,
)
from apps.members.models import (
    BUILTIN_ROLE_PERMISSIONS,
    CHANNEL_CAPABILITIES,
    ChannelAssignment,
    WorkspaceMembership,
    role_rank,
)
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def other_workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand B")


def make_member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    return WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)


def make_channel(workspace, handle):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle=handle,
        account_name=handle,
        account_platform_id=handle,
    )


@pytest.mark.django_db
class TestVisibility:
    def test_owner_sees_every_channel_without_assignment(self, django_user_model, workspace):
        owner = make_member(django_user_model, workspace, "owner", "owner@example.com")
        make_channel(workspace, "one")
        make_channel(workspace, "two")
        assert visible_accounts(owner).count() == 2

    @pytest.mark.parametrize("role", ["manager", "editor", "reviewer", "viewer"])
    def test_everyone_else_starts_with_nothing(self, django_user_model, workspace, role):
        """Including manager. A new member should not silently inherit the
        whole workspace."""
        member = make_member(django_user_model, workspace, role, f"{role}@example.com")
        make_channel(workspace, "one")
        assert visible_accounts(member).count() == 0

    def test_an_assignment_reveals_exactly_one_channel(self, django_user_model, workspace):
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        mine = make_channel(workspace, "mine")
        make_channel(workspace, "theirs")
        ChannelAssignment.objects.create(membership=member, social_account=mine)
        visible = visible_accounts(member)
        assert [a.account_handle for a in visible] == ["mine"]

    def test_the_reviewer_case_from_the_plan(self, django_user_model, workspace):
        """A personal-account owner: reviewer role, one assigned channel. Sees
        and approves their own, and nothing else in the workspace."""
        reviewer = make_member(django_user_model, workspace, "reviewer", "personal@example.com")
        theirs = make_channel(workspace, "their-own")
        make_channel(workspace, "the-agency-brand")
        ChannelAssignment.objects.create(membership=reviewer, social_account=theirs, can_approve=True)
        assert [a.account_handle for a in visible_accounts(reviewer)] == ["their-own"]
        assert reviewer.effective_permissions["approve_posts"] is True
        assert reviewer.effective_permissions["create_posts"] is False

    def test_no_membership_sees_nothing(self):
        assert visible_accounts(None).count() == 0


@pytest.mark.django_db
class TestCapabilities:
    def test_owner_may_do_anything_in_their_workspace(self, django_user_model, workspace):
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        channel = make_channel(workspace, "c")
        for capability in CHANNEL_CAPABILITIES:
            assert can_on_channel(owner, channel, capability)

    def test_an_assignment_grants_only_what_it_says(self, django_user_model, workspace):
        """Six independent flags, not a ladder: answering comments all day must
        not imply being able to delete them."""
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        channel = make_channel(workspace, "c")
        ChannelAssignment.objects.create(membership=member, social_account=channel, can_reply=True)
        assert can_on_channel(member, channel, "can_reply")
        assert not can_on_channel(member, channel, "can_delete_comments")
        assert not can_on_channel(member, channel, "can_publish")

    def test_unassigned_channel_grants_nothing(self, django_user_model, workspace):
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        channel = make_channel(workspace, "c")
        assert not can_on_channel(member, channel, "can_reply")

    def test_owning_one_workspace_is_not_owning_another(self, django_user_model, workspace, other_workspace):
        """The check that stops an owner reaching into a workspace they do not
        belong to."""
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        foreign = make_channel(other_workspace, "not-theirs")
        assert not can_on_channel(owner, foreign, "can_reply")
        assert not sees_all_channels(None)

    def test_an_unknown_capability_is_an_error_not_a_denial(self, django_user_model, workspace):
        """Silently returning False would turn a typo into a permanent, silent
        denial that nobody ever debugs."""
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        channel = make_channel(workspace, "c")
        with pytest.raises(ValueError, match="Unknown channel capability"):
            can_on_channel(owner, channel, "can_teleport")


@pytest.mark.django_db
class TestDelegation:
    def test_only_owner_and_manager_may_assign(self, django_user_model, workspace):
        for role, expected in [
            ("owner", True),
            ("manager", True),
            ("editor", False),
            ("reviewer", False),
            ("viewer", False),
        ]:
            member = make_member(django_user_model, workspace, role, f"{role}-d@example.com")
            assert can_assign_channels(member) is expected, role

    def test_a_manager_can_only_pass_on_what_they_hold(self, django_user_model, workspace):
        """Otherwise delegation manufactures access the delegator never had."""
        manager = make_member(django_user_model, workspace, "manager", "m@example.com")
        held = make_channel(workspace, "held")
        make_channel(workspace, "not-held")
        ChannelAssignment.objects.create(membership=manager, social_account=held)
        assert [a.account_handle for a in assignable_accounts(manager)] == ["held"]

    def test_an_owner_may_assign_anything_in_the_workspace(self, django_user_model, workspace):
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        make_channel(workspace, "a")
        make_channel(workspace, "b")
        assert assignable_accounts(owner).count() == 2

    def test_assignment_targets_must_rank_strictly_below(self, django_user_model, workspace):
        """Two managers editing each other's access is a loop with no adult in
        the room, and nobody is below themselves."""
        manager = make_member(django_user_model, workspace, "manager", "m1@example.com")
        peer = make_member(django_user_model, workspace, "manager", "m2@example.com")
        editor = make_member(django_user_model, workspace, "editor", "e@example.com")
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        assert can_assign_to(manager, editor)
        assert not can_assign_to(manager, peer)
        assert not can_assign_to(manager, owner)
        assert not can_assign_to(manager, manager)
        assert can_assign_to(owner, manager)

    def test_cannot_assign_across_workspaces(self, django_user_model, workspace, other_workspace):
        owner = make_member(django_user_model, workspace, "owner", "o@example.com")
        stranger = make_member(django_user_model, other_workspace, "editor", "s@example.com")
        assert not can_assign_to(owner, stranger)


@pytest.mark.django_db
class TestModelIntegrity:
    def test_a_member_cannot_be_assigned_another_workspaces_channel(
        self, django_user_model, workspace, other_workspace
    ):
        """Nothing in the schema prevents this pairing, and it would quietly
        hand someone another brand's inbox."""
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        foreign = make_channel(other_workspace, "other-brand")
        assignment = ChannelAssignment(membership=member, social_account=foreign)
        with pytest.raises(ValidationError, match="different workspace"):
            assignment.clean()

    def test_one_assignment_per_member_and_channel(self, django_user_model, workspace):
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        channel = make_channel(workspace, "c")
        ChannelAssignment.objects.create(membership=member, social_account=channel)
        with pytest.raises(IntegrityError):
            ChannelAssignment.objects.create(membership=member, social_account=channel)

    def test_granted_lists_only_what_is_on(self, django_user_model, workspace):
        member = make_member(django_user_model, workspace, "editor", "e@example.com")
        channel = make_channel(workspace, "c")
        assignment = ChannelAssignment.objects.create(
            membership=member, social_account=channel, can_reply=True, can_publish=True
        )
        assert sorted(assignment.granted) == ["can_publish", "can_reply"]


class TestReviewerRole:
    def test_reviewer_sits_between_editor_and_viewer(self):
        assert role_rank("editor") > role_rank("reviewer") > role_rank("viewer")

    def test_reviewer_approves_but_does_not_create(self):
        perms = BUILTIN_ROLE_PERMISSIONS["reviewer"]
        assert perms["approve_posts"] is True
        assert perms["create_posts"] is False
        assert perms["publish_directly"] is False
        # Reads the inbox to review in context, but is not the voice of the
        # channel.
        assert perms["use_inbox"] is True
        assert perms["reply_from_inbox"] is False

    def test_every_role_declares_every_permission(self):
        """A missing key reads as False, which is a denial nobody wrote down."""
        keys = set(BUILTIN_ROLE_PERMISSIONS["owner"])
        for role, perms in BUILTIN_ROLE_PERMISSIONS.items():
            assert set(perms) == keys, f"{role} is missing {keys - set(perms)}"

    def test_reviewer_is_offered_in_the_role_picker(self):
        from apps.members.models import assignable_workspace_role_choices

        assert "reviewer" in dict(assignable_workspace_role_choices())
