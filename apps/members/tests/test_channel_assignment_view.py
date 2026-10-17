"""The screen where channel access is handed out.

Two authorisations have to hold, and they answer different questions: *may you
assign at all* (the owner, and only the owner), and *may you assign to this
person* (strictly below you). The second is kept even though the first now
allows just one role, because it is what stops an owner editing their own access
or a co-owner's — and it is the check that survives if assigning is ever widened.
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
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, membership


def url(workspace, membership):
    return reverse(
        "members:manage_channels",
        kwargs={"workspace_id": workspace.id, "membership_id": membership.id},
    )


@pytest.mark.django_db
class TestWhoMayOpenIt:
    def test_an_owner_may_assign_to_an_editor(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(owner_user)
        assert client.get(url(workspace, editor)).status_code == 200

    def test_an_editor_may_not_assign_at_all(self, client, django_user_model, workspace, channel):
        editor_user, _ = member(django_user_model, workspace, "editor", "e1@example.com")
        _, other = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(editor_user)
        assert client.get(url(workspace, other)).status_code == 403

    def test_a_manager_may_assign_to_someone_below_them(self, client, django_user_model, workspace, channel):
        manager_user, manager = member(django_user_model, workspace, "manager", "m@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=manager, social_account=channel)
        client.force_login(manager_user)
        assert client.get(url(workspace, editor)).status_code == 200

    def test_a_manager_may_not_edit_a_peer(self, client, django_user_model, workspace, channel):
        """Equal ranks refused on purpose — otherwise it is a loop with no adult
        in the room."""
        manager_user, _ = member(django_user_model, workspace, "manager", "m1@example.com")
        _, peer = member(django_user_model, workspace, "manager", "m2@example.com")
        client.force_login(manager_user)
        assert client.get(url(workspace, peer)).status_code == 403

    def test_a_manager_may_not_edit_the_owner(self, client, django_user_model, workspace, channel):
        manager_user, _ = member(django_user_model, workspace, "manager", "m@example.com")
        _, owner = member(django_user_model, workspace, "owner", "o@example.com")
        client.force_login(manager_user)
        assert client.get(url(workspace, owner)).status_code == 403


@pytest.mark.django_db
class TestWhatItOffers:
    def test_an_owner_is_offered_every_channel_in_the_workspace(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        SocialAccount.objects.create(
            workspace=workspace,
            platform="tiktok",
            account_handle="second",
            account_name="Second",
            account_platform_id="second",
        )
        client.force_login(owner_user)
        response = client.get(url(workspace, editor))
        offered = {row["channel"].account_handle for row in response.context["rows"]}
        assert offered == {"the-channel", "second"}

    def test_a_manager_is_only_offered_channels_they_hold(self, client, django_user_model, workspace, channel):
        """Delegation must not manufacture access the delegator never had."""
        manager_user, manager = member(django_user_model, workspace, "manager", "m@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        SocialAccount.objects.create(
            workspace=workspace,
            platform="tiktok",
            account_handle="not-held",
            account_name="Not Held",
            account_platform_id="not-held",
        )
        ChannelAssignment.objects.create(membership=manager, social_account=channel)
        client.force_login(manager_user)
        response = client.get(url(workspace, editor))
        offered = {row["channel"].account_handle for row in response.context["rows"]}
        assert offered == {"the-channel"}


@pytest.mark.django_db
class TestSaving:
    def test_ticking_a_channel_grants_the_boxes_ticked_with_it(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(owner_user)
        client.post(
            url(workspace, editor),
            {f"channel_{channel.id}": "1", f"can_reply_{channel.id}": "1"},
        )
        assignment = ChannelAssignment.objects.get(membership=editor, social_account=channel)
        assert assignment.can_reply is True
        assert assignment.can_delete_comments is False

    def test_unticking_removes_access_entirely(self, client, django_user_model, workspace, channel):
        """The row existing is the view grant, so keeping it with every flag off
        would leave them still seeing the channel — which is not what unticking
        looks like it does."""
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=editor, social_account=channel, can_reply=True)
        client.force_login(owner_user)
        client.post(url(workspace, editor), {})
        assert not ChannelAssignment.objects.filter(membership=editor).exists()

    def test_saving_records_who_granted_it(self, client, django_user_model, workspace, channel):
        """The activity log will want this, and it cannot be reconstructed later."""
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(owner_user)
        client.post(url(workspace, editor), {f"channel_{channel.id}": "1"})
        assignment = ChannelAssignment.objects.get(membership=editor)
        assert assignment.assigned_by == owner_user

    def test_a_second_save_updates_rather_than_duplicates(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        _, editor = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(owner_user)
        client.post(url(workspace, editor), {f"channel_{channel.id}": "1", f"can_reply_{channel.id}": "1"})
        client.post(url(workspace, editor), {f"channel_{channel.id}": "1", f"can_publish_{channel.id}": "1"})
        assignments = ChannelAssignment.objects.filter(membership=editor)
        assert assignments.count() == 1
        assignment = assignments.first()
        assert assignment.can_publish is True
        assert assignment.can_reply is False


def only_org(user, organization, org_role=OrgMembership.OrgRole.OWNER):
    """Make ``organization`` the user's only one, at ``org_role``.

    Creating a user fires a signal that provisions them a personal org, and
    ``request.org`` resolves to the earliest membership — so without this the
    viewer lands in their own org and the member list is empty. Dropping the
    personal one is what makes the request run against the fixture's org.

    The org role matters separately from the workspace role: the member-admin
    views are gated on the org scale (owner/admin/member), so a workspace owner
    who is only an org member still cannot reach them.
    """
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()
    OrgMembership.objects.filter(user=user, organization=organization).update(org_role=org_role)


def flags_for(response):
    """Every ``can_manage_channels`` the row template will branch on."""
    return [
        getattr(wm, "can_manage_channels", None)
        for entry in response.context["members_data"]
        for wm in entry["workspace_memberships"]
    ]


@pytest.mark.django_db
class TestTheWayIn:
    """The screen is useless if nothing links to it, and a link that always
    403s is worse than none — so the badge is only a button where the viewer
    can actually assign.

    Asserted on the flag the template branches on, not on rendered HTML: the
    markup will be restyled, the decision will not.
    """

    def test_an_editor_gets_no_buttons_at_all(self, client, django_user_model, workspace, channel):
        editor_user, _ = member(django_user_model, workspace, "editor", "e1@example.com")
        member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(editor_user)
        response = client.get(reverse("members:list"))
        assert response.status_code == 200
        assert not any(flags_for(response))

    def test_an_owner_gets_a_button_for_someone_below_them(self, client, django_user_model, workspace, channel):
        """The positive case. It used to be skipped, and skipping it is why the
        bug below went unnoticed for so long: an attribute that is *never* set
        satisfies `assert not any(flags)` just as well as one correctly withheld.
        """
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        only_org(owner_user, workspace.organization)
        member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(owner_user)
        response = client.get(reverse("members:list"))
        assert response.status_code == 200
        assert any(flags_for(response))

    def test_an_owner_gets_no_button_for_their_own_row(self, client, django_user_model, workspace, channel):
        """Offering it would be offering something `can_assign_to` then refuses:
        the rule is strictly *below* you, and you are not below yourself."""
        owner_user, owner_ws = member(django_user_model, workspace, "owner", "o@example.com")
        only_org(owner_user, workspace.organization)
        client.force_login(owner_user)
        response = client.get(reverse("members:list"))
        own = [
            wm.can_manage_channels
            for entry in response.context["members_data"]
            for wm in entry["workspace_memberships"]
            if wm.id == owner_ws.id
        ]
        assert own == [False]


@pytest.mark.django_db
class TestTheWayInSurvivesAChange:
    """Every view that redraws a member row has to re-annotate it.

    `can_manage_channels` is a runtime attribute, not a model field, so a view
    that refetches the memberships from the database and renders the same
    partial gets a falsy value and the badge degrades to an inert span. Because
    the row is swapped `outerHTML`, the teleported dialog goes with it — so
    changing someone's role looked exactly like it had revoked their channel
    access, and only a full page reload brought it back.

    One test per view that renders `member_row.html`, since each one had its own
    copy of the omission.
    """

    def test_after_changing_the_role(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        only_org(owner_user, workspace.organization)
        editor_user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        target = OrgMembership.objects.get(user=editor_user, organization=workspace.organization)
        client.force_login(owner_user)

        response = client.post(
            reverse("members:update_role", kwargs={"membership_id": target.id}),
            {"workspace_role": "viewer"},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        entry = response.context["member"]
        assert [wm.can_manage_channels for wm in entry["workspace_memberships"]] == [True]

    def test_after_saving_workspace_assignments(self, client, django_user_model, workspace, channel):
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        only_org(owner_user, workspace.organization)
        editor_user, _ = member(django_user_model, workspace, "editor", "e@example.com")
        target = OrgMembership.objects.get(user=editor_user, organization=workspace.organization)
        client.force_login(owner_user)

        response = client.post(
            reverse("members:manage_workspaces", kwargs={"membership_id": target.id}),
            {f"ws_{workspace.id}": "1"},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        entry = response.context["member"]
        assert [wm.can_manage_channels for wm in entry["workspace_memberships"]] == [True]

    def test_after_adding_someone_who_already_has_an_account(self, client, django_user_model, workspace, channel):
        """The direct-add path appends a row without a page reload, so it needs
        the annotation too or the new member arrives with no way in."""
        owner_user, _ = member(django_user_model, workspace, "owner", "o@example.com")
        only_org(owner_user, workspace.organization)
        newcomer = django_user_model.objects.create_user(
            email="new@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=newcomer).delete()
        client.force_login(owner_user)

        response = client.post(
            reverse("members:invite"),
            {
                "email": newcomer.email,
                "workspace_role": "editor",
                f"ws_{workspace.id}": "1",
            },
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        entry = response.context["member"]
        assert [wm.can_manage_channels for wm in entry["workspace_memberships"]] == [True]
