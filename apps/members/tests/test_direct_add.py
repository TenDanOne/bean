"""Inviting an address that already has an account adds it directly.

The email round-trip is dead weight when the invitee can already log in, and a
delivery failure leaves the admin with nothing. These tests pin the direct-add
path, the org collapse it depends on, and — most importantly — that it enforces
the same role hierarchy as the email path, since a second grant path that skipped
those checks would be a privilege-escalation hole.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.services import INVITE_EXPIRY_DAYS
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _make_user(email):
    """A user with the org + workspace that the accounts post_save signal provisions."""
    return User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())


def _strip_auto_org(user):
    """Detach a user from the auto-provisioned org so they can be placed in a specific one."""
    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class DirectAddTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Agency Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS-A")

        self.owner = _strip_auto_org(_make_user("owner@example.com"))
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")

        self.url = reverse("members:invite")
        self.client.force_login(self.owner)

    def _invite(self, email, **extra):
        data = {"email": email, "workspace_role": "viewer"}
        data.update(extra)
        return self.client.post(self.url, data=data, headers={"HX-Request": "true"})

    def test_existing_user_is_added_immediately_without_an_invitation(self):
        invitee = _make_user("already@example.com")

        response = self._invite(
            "already@example.com",
            workspace_role="editor",
            **{f"ws_{self.workspace.id}": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(OrgMembership.objects.filter(user=invitee, organization=self.org).exists())
        self.assertTrue(
            WorkspaceMembership.objects.filter(user=invitee, workspace=self.workspace, workspace_role="editor").exists()
        )
        # No pending invite is left behind — they never needed one.
        self.assertFalse(Invitation.objects.filter(email="already@example.com").exists())

    def test_direct_add_retargets_the_swap_to_the_members_list(self):
        """The modal posts at #invitations-list; a new member belongs in #members-list."""
        _make_user("already@example.com")

        response = self._invite("already@example.com")

        self.assertEqual(response["HX-Retarget"], "#members-list")
        self.assertEqual(response["HX-Reswap"], "beforeend")

    def test_returned_row_renders_the_role_dropdown(self):
        """The row's role <select> is populated from workspace_role_choices,
        which the swapped-in response has to supply or the dropdown comes back
        empty."""
        _make_user("already@example.com")

        html = self._invite("already@example.com").content.decode()

        self.assertIn("already@example.com", html)
        self.assertIn('value="editor"', html)

    def test_direct_add_collapses_the_users_own_org(self):
        """Signup provisions everyone a personal org; two memberships would make
        RBACMiddleware's single-org lookup nondeterministic."""
        invitee = _make_user("already@example.com")
        own_org_id = OrgMembership.objects.get(user=invitee).organization_id

        self._invite("already@example.com")

        memberships = OrgMembership.objects.filter(user=invitee)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.first().organization_id, self.org.id)
        self.assertFalse(Organization.objects.filter(id=own_org_id).exists())
        self.assertFalse(Workspace.objects.filter(organization_id=own_org_id).exists())

    def test_collapse_destroys_data_in_the_absorbed_org(self):
        """Documents the deliberate trade-off: absorption is unconditional."""
        invitee = _make_user("already@example.com")
        own_workspace = Workspace.objects.get(organization__memberships__user=invitee)
        SocialAccount.objects.create(
            workspace=own_workspace,
            platform="youtube",
            account_platform_id="UC-doomed",
            account_name="Their channel",
        )

        self._invite("already@example.com")

        self.assertFalse(SocialAccount.objects.filter(account_platform_id="UC-doomed").exists())

    def test_existing_member_is_rejected_not_duplicated(self):
        invitee = _strip_auto_org(_make_user("already@example.com"))
        OrgMembership.objects.create(user=invitee, organization=self.org, org_role="member")

        response = self._invite("already@example.com")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(OrgMembership.objects.filter(user=invitee, organization=self.org).count(), 1)

    def test_email_match_is_case_insensitive(self):
        invitee = _make_user("already@example.com")

        self._invite("ALREADY@Example.COM")

        self.assertTrue(OrgMembership.objects.filter(user=invitee, organization=self.org).exists())

    def test_unknown_email_still_creates_an_invitation(self):
        response = self._invite("newcomer@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Retarget", response)
        self.assertTrue(Invitation.objects.filter(email="newcomer@example.com").exists())


class DirectAddRoleHierarchyTests(TestCase):
    """The direct-add path must not become a way around the invite checks."""

    def setUp(self):
        self.org = Organization.objects.create(name="Agency Org")
        self.workspace_a = Workspace.objects.create(organization=self.org, name="WS-A")
        self.workspace_b = Workspace.objects.create(organization=self.org, name="WS-B")

        self.admin = _strip_auto_org(_make_user("admin@example.com"))
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace_a, workspace_role="viewer")

        self.invitee = _make_user("already@example.com")
        self.url = reverse("members:invite")
        self.client.force_login(self.admin)

    def _invite(self, **extra):
        data = {"email": "already@example.com", "workspace_role": "viewer"}
        data.update(extra)
        return self.client.post(self.url, data=data, headers={"HX-Request": "true"})

    def test_the_org_role_is_never_taken_from_the_request(self):
        """The two org-escalation tests this replaces posted org_role=admin and
        org_role=owner and expected a refusal. The view no longer reads the
        field at all, so there is nothing to refuse — everyone joins as a member.
        Posting it by hand is now inert, which is the stronger property.
        """
        response = self._invite(org_role="admin")

        self.assertLess(response.status_code, 400)
        membership = OrgMembership.objects.get(user=self.invitee, organization=self.org)
        self.assertEqual(membership.org_role, "member")

    def test_admin_cannot_grant_a_role_above_their_own(self):
        """The admin is only a viewer in WS-A."""
        response = self._invite(workspace_role="manager", **{f"ws_{self.workspace_a.id}": "1"})

        self.assertEqual(response.status_code, 422)
        self.assertFalse(OrgMembership.objects.filter(user=self.invitee, organization=self.org).exists())

    def test_nobody_can_grant_workspace_ownership(self):
        response = self._invite(workspace_role="owner", **{f"ws_{self.workspace_a.id}": "1"})

        self.assertEqual(response.status_code, 422)
        self.assertFalse(OrgMembership.objects.filter(user=self.invitee, organization=self.org).exists())

    def test_admin_cannot_grant_access_to_a_workspace_they_dont_belong_to(self):
        response = self._invite(**{f"ws_{self.workspace_b.id}": "1"})

        self.assertEqual(response.status_code, 422)
        self.assertFalse(WorkspaceMembership.objects.filter(user=self.invitee, workspace=self.workspace_b).exists())

    def test_a_rejected_direct_add_leaves_the_users_own_org_intact(self):
        """Validation runs before absorption — a refused invite must not have destroyed anything."""
        own_org_id = OrgMembership.objects.get(user=self.invitee).organization_id

        self._invite(workspace_role="manager", **{f"ws_{self.workspace_a.id}": "1"})

        self.assertTrue(Organization.objects.filter(id=own_org_id).exists())
        self.assertEqual(OrgMembership.objects.filter(user=self.invitee).count(), 1)


class AcceptInviteAsRegisteredUserTests(TestCase):
    """Regression: accepting an invite while already registered left the user in two orgs."""

    def test_accepting_collapses_the_previous_org(self):
        from datetime import timedelta

        inviting_org = Organization.objects.create(name="Agency Org")
        workspace = Workspace.objects.create(organization=inviting_org, name="WS-A")

        user = _make_user("already@example.com")
        own_org_id = OrgMembership.objects.get(user=user).organization_id

        invitation = Invitation.objects.create(
            organization=inviting_org,
            email="already@example.com",
            org_role="member",
            workspace_assignments=[{"workspace_id": str(workspace.id), "role": "editor"}],
            expires_at=timezone.now() + timedelta(days=INVITE_EXPIRY_DAYS),
        )

        self.client.force_login(user)
        self.client.post(reverse("members:accept_invite", kwargs={"token": invitation.token}))

        memberships = OrgMembership.objects.filter(user=user)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.first().organization_id, inviting_org.id)
        self.assertFalse(Organization.objects.filter(id=own_org_id).exists())
