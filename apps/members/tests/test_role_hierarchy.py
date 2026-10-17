"""HTTP-level tests for invite / role-management hierarchy enforcement.

Covers V1 from the May-2026 security audit: an org admin must not be able to
invite users with org/workspace roles above their own, nor demote an org owner.
"""

from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _login(client, user):
    client.force_login(user)


def _make_user(email):
    user = User.objects.create_user(
        email=email,
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    # The accounts post_save signal auto-provisions a default Organization +
    # Workspace + OrgMembership for every new User. Tests that want to attach
    # the user to a specific org must start from a clean slate, otherwise the
    # RBAC middleware (which does OrgMembership.objects.filter(...).first())
    # may pick the auto-org instead of the test org.
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization

    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class InviteRoleHierarchyTests(TestCase):
    """POST /members/invite/ must enforce role-hierarchy."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace_a = Workspace.objects.create(organization=self.org, name="WS-A")
        self.workspace_b = Workspace.objects.create(organization=self.org, name="WS-B")

        self.owner = _make_user("owner@example.com")
        self.admin = _make_user("admin@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")
        # Admin is a viewer in WS-A (cannot grant owner there) and not a member of WS-B.
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace_a, workspace_role="viewer")

        self.url = reverse("members:invite")

    def test_admin_cannot_invite_above_their_own_role_in_that_workspace(self):
        """The admin is only a viewer in WS-A, so manager is above them there."""
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "victim@example.com",
                "workspace_role": "manager",
                f"ws_{self.workspace_a.id}": "1",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="victim@example.com").exists())

    def test_admin_cannot_invite_into_workspace_they_dont_belong_to(self):
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "victim@example.com",
                "workspace_role": "viewer",
                f"ws_{self.workspace_b.id}": "1",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="victim@example.com").exists())

    def test_the_org_role_cannot_be_chosen_at_all(self):
        """Everyone invited joins as a member of the account.

        The org role said who administers billing and the team list, never what
        someone could do with the content, and offering it beside the real role
        read as one ladder. It is no longer read from the request — posting it
        by hand changes nothing, which is what this pins.
        """
        _login(self.client, self.owner)
        response = self.client.post(
            self.url,
            data={
                "email": "sneaky@example.com",
                "org_role": "admin",
                "workspace_role": "viewer",
            },
        )
        self.assertLess(response.status_code, 400)
        invitation = Invitation.objects.get(email="sneaky@example.com")
        self.assertEqual(invitation.org_role, "member")

    def test_owner_can_invite_at_the_top_of_what_is_grantable(self):
        _login(self.client, self.owner)
        response = self.client.post(
            self.url,
            data={
                "email": "legit@example.com",
                # Manager is the top of what may be handed out; see below.
                "workspace_role": "manager",
                f"ws_{self.workspace_a.id}": "1",
            },
        )
        # 200 (HTML redirect) or 302 — anything < 400 means accepted. The view
        # returns redirect to members:list for non-HTMX flows.
        self.assertLess(response.status_code, 400)
        self.assertTrue(Invitation.objects.filter(email="legit@example.com").exists())

    def test_not_even_an_owner_can_invite_someone_as_workspace_owner(self):
        """A workspace owner sees every channel with no assignment, so granting
        the role is the one move that undoes channel access wholesale. It is set
        when the workspace is created and moved by transfer, never handed out —
        which is why this is refused for the org owner too, not just below them.
        """
        _login(self.client, self.owner)
        response = self.client.post(
            self.url,
            data={
                "email": "escalation@example.com",
                "workspace_role": "owner",
                f"ws_{self.workspace_a.id}": "1",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="escalation@example.com").exists())

    def test_admin_with_manager_role_can_invite_editor(self):
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace_b, workspace_role="manager")
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "editor@example.com",
                "workspace_role": "editor",
                f"ws_{self.workspace_b.id}": "1",
            },
        )
        self.assertLess(response.status_code, 400)
        self.assertTrue(Invitation.objects.filter(email="editor@example.com").exists())


class RolePickerTests(TestCase):
    """GET /members/ — one picker, offering the roles that mean something.

    This replaces a pair of tests that checked the Admin org-role option was
    shown to owners and hidden from admins. That control is gone: there is no
    org-role picker anywhere, so there is nothing left to gate.
    """

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.owner = _make_user("owner@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        self.url = reverse("members:list")

    def test_the_admin_option_is_gone_entirely(self):
        _login(self.client, self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="admin"')
        self.assertNotContains(response, 'name="org_role"')

    def test_the_real_roles_are_offered(self):
        _login(self.client, self.owner)
        response = self.client.get(self.url)
        for role in ("manager", "editor", "reviewer", "viewer"):
            self.assertContains(response, f'value="{role}"')

    def test_workspace_owner_is_not_offered(self):
        _login(self.client, self.owner)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'value="owner"')


class ManageWorkspaceModalTargetTests(TestCase):
    """GET /members/ renders one HTMX modal target per manageable member."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.owner = _make_user("owner@example.com")
        self.member_a = _make_user("member-a@example.com")
        self.member_b = _make_user("member-b@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        self.member_a_membership = OrgMembership.objects.create(
            user=self.member_a,
            organization=self.org,
            org_role="member",
        )
        self.member_b_membership = OrgMembership.objects.create(
            user=self.member_b,
            organization=self.org,
            org_role="member",
        )
        self.url = reverse("members:list")

    def test_manage_workspace_targets_are_unique_per_member(self):
        html = "".join(
            render_to_string(
                "members/partials/member_row.html",
                {
                    "member": {
                        "membership": membership,
                        "user": membership.user,
                        "workspace_memberships": [],
                        # Per-row now, not a page-wide `is_admin`: a manager
                        # manages some of the people listed and not others.
                        "can_manage": True,
                        "can_remove": True,
                    },
                    "current_user": self.owner,
                    "workspace_role_choices": WorkspaceMembership.WorkspaceRole.choices,
                },
            )
            for membership in (self.member_a_membership, self.member_b_membership)
        )

        self.assertNotIn('id="ws-modal-content"', html)
        for membership in (self.member_a_membership, self.member_b_membership):
            target_id = f"ws-modal-content-{membership.id}"
            self.assertIn(f'hx-target="#{target_id}"', html)
            self.assertIn(f'id="{target_id}"', html)


class UpdateMemberRoleHierarchyTests(TestCase):
    """POST /members/<id>/role/ — admin cannot demote owner."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.owner = _make_user("owner@example.com")
        self.other_owner = _make_user("owner2@example.com")
        self.admin = _make_user("admin@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        self.other_owner_membership = OrgMembership.objects.create(
            user=self.other_owner, organization=self.org, org_role="owner"
        )
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")

    def test_admin_cannot_demote_owner(self):
        _login(self.client, self.admin)
        url = reverse("members:update_role", kwargs={"membership_id": self.other_owner_membership.id})
        response = self.client.post(url, data={"org_role": "admin"})
        self.assertEqual(response.status_code, 422)
        self.other_owner_membership.refresh_from_db()
        self.assertEqual(self.other_owner_membership.org_role, "owner")

    def test_admin_cannot_promote_member_to_admin(self):
        # Lateral promotion to admin requires owner privileges.
        member = _make_user("member@example.com")
        membership = OrgMembership.objects.create(user=member, organization=self.org, org_role="member")
        _login(self.client, self.admin)
        url = reverse("members:update_role", kwargs={"membership_id": membership.id})
        response = self.client.post(url, data={"org_role": "admin"})
        self.assertEqual(response.status_code, 422)
        membership.refresh_from_db()
        self.assertEqual(membership.org_role, "member")


class ManageWorkspacesExistingRoleTests(TestCase):
    """POST /members/<id>/workspaces/ — caller must outrank the *current*
    workspace role too, otherwise a viewer-level admin could silently demote
    an owner by submitting role="viewer" (Codex regression test)."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.admin = _make_user("admin@example.com")
        self.victim = _make_user("victim@example.com")
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")
        OrgMembership.objects.create(user=self.victim, organization=self.org, org_role="member")
        # admin is only a viewer in the workspace; victim is the owner.
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace, workspace_role="viewer")
        self.victim_ws_membership = WorkspaceMembership.objects.create(
            user=self.victim, workspace=self.workspace, workspace_role="owner"
        )
        self.victim_org_membership = OrgMembership.objects.get(user=self.victim, organization=self.org)

    def test_the_workspace_form_cannot_demote_an_owner_at_all(self):
        """It no longer carries a role, and a ticked owner stays an owner.

        The form used to have a role picker per workspace, and the guard here
        was that a viewer-level caller could not use it to demote the owner.
        Now the form only decides *which* brands someone works on, and the view
        preserves an existing owner rather than writing the caller's chosen role
        over it — so the demotion is not refused, it is not expressible.
        """
        _login(self.client, self.admin)
        url = reverse(
            "members:manage_workspaces",
            kwargs={"membership_id": self.victim_org_membership.id},
        )
        response = self.client.post(url, data={f"ws_{self.workspace.id}": "1", "ws_role_x": "viewer"})
        self.assertLess(response.status_code, 400)
        self.victim_ws_membership.refresh_from_db()
        self.assertEqual(self.victim_ws_membership.workspace_role, "owner")

    def test_viewer_level_admin_cannot_remove_owner_via_workspace_form(self):
        # Unchecking the workspace box deletes the membership — also a state
        # change the lower-tier caller must not be allowed to perform.
        _login(self.client, self.admin)
        url = reverse(
            "members:manage_workspaces",
            kwargs={"membership_id": self.victim_org_membership.id},
        )
        # No ws_<id> key in POST = "remove this workspace assignment".
        response = self.client.post(url, data={})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(WorkspaceMembership.objects.filter(id=self.victim_ws_membership.id).exists())

    def test_an_owner_peer_still_cannot_demote_through_this_form(self):
        """Even with the authority, the form is not where roles change.

        Demotion happens through the member's own role picker, which writes one
        role across their brands; ownership itself moves by transfer.
        """
        WorkspaceMembership.objects.filter(user=self.admin, workspace=self.workspace).update(workspace_role="owner")
        _login(self.client, self.admin)
        url = reverse(
            "members:manage_workspaces",
            kwargs={"membership_id": self.victim_org_membership.id},
        )
        response = self.client.post(url, data={f"ws_{self.workspace.id}": "1"})
        self.assertLess(response.status_code, 400)
        self.victim_ws_membership.refresh_from_db()
        self.assertEqual(self.victim_ws_membership.workspace_role, "owner")


class ClientPortalManagerInviteTests(TestCase):
    """POST /workspace/<id>/settings/clients/invite/ — a workspace manager who
    is only org-role=member must still be able to invite clients (org_role=member).
    Regression for the Codex P1 finding."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.manager = _make_user("manager@example.com")
        OrgMembership.objects.create(user=self.manager, organization=self.org, org_role="member")
        WorkspaceMembership.objects.create(user=self.manager, workspace=self.workspace, workspace_role="manager")

    def test_workspace_manager_can_invite_client(self):
        _login(self.client, self.manager)
        url = reverse("client_portal_admin:invite_client", kwargs={"workspace_id": self.workspace.id})
        response = self.client.post(url, data={"email": "newclient@example.com"})
        # 200 (HTMX) or 302 (regular redirect) both indicate success.
        self.assertLess(response.status_code, 400)
        from apps.members.models import Invitation

        self.assertTrue(Invitation.objects.filter(email="newclient@example.com").exists())
