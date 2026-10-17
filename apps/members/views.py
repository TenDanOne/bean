"""Views for team member management."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.members.channels import can_assign_channels, can_manage_member
from apps.workspaces.models import Workspace

from . import services
from .decorators import require_org_role
from .models import (
    CHANNEL_CAPABILITIES,
    CHANNEL_CAPABILITY_LABELS,
    ChannelAssignment,
    Invitation,
    OrgMembership,
    WorkspaceMembership,
    assignable_workspace_role_choices,
    assignable_workspace_role_descriptions,
    capabilities_available_to,
    role_rank,
)


def _annotate_channel_access(request, entries):
    """Mark which workspace badges may open the channel-access dialog.

    Sets ``can_manage_channels`` on each ``WorkspaceMembership`` instance in
    ``entries``. It is a runtime attribute, not a model field, so **every** view
    that renders ``member_row.html`` has to call this — which is exactly what
    went wrong before this existed: the annotation lived inline in
    ``member_list`` and the three other views that re-render the row omitted it.
    The template then read a missing attribute as falsy, the badge degraded to an
    inert span, and since the row is swapped ``outerHTML`` the dialog went with
    it. Changing someone's role appeared to revoke channel access.

    The member list is org-scoped, so nothing here knows the viewer's role *per
    workspace* — without this the badges would link to a page that 403s, and a
    link that always fails is worse than no link.
    """
    entries = list(entries)
    if not entries:
        return entries

    # The viewer's own role in each brand, so the rank comparison below has
    # something to compare against. It used to collapse to a set of "brands I
    # may assign in", which answered only half the question — the comment here
    # claimed "strictly below the viewer" while the code never compared the two
    # roles. A manager was therefore offered the button on the **owner's** row,
    # and `manage_channels` refused it on the click.
    viewer_roles = {
        wm.workspace_id: wm
        for wm in WorkspaceMembership.objects.filter(user=request.user, workspace__organization=request.org)
    }
    for entry in entries:
        is_self = entry["user"].id == request.user.id
        for wm in entry["workspace_memberships"]:
            viewer = viewer_roles.get(wm.workspace_id)
            wm.can_manage_channels = (
                not is_self
                and viewer is not None
                and can_assign_channels(viewer)
                # Strictly below, the same rule `can_assign_to` enforces in the
                # view. The owner is above everyone, so their row stays inert —
                # they are the one who connects the channels and hands them out,
                # and a manager editing that would invert the arrangement.
                and role_rank(viewer.workspace_role) > role_rank(wm.workspace_role)
            )
    return entries


def _manageable_user_ids(request, users):
    """The subset of ``users`` this viewer may manage, as a set of ids.

    `can_manage_member` is a couple of queries; the team page lists everyone, so
    calling it per row would be that many times over. Org admins short-circuit
    to "all of them" because that is exactly what the helper concludes for them.
    """
    if _is_org_admin(request):
        return {u.id for u in users if u.id != request.user.id}
    return {u.id for u in users if can_manage_member(request.user, request.org, u)}


def _is_org_admin(request):
    """Administers the account: invites, removals, the org's own settings.

    A separate question from managing a brand, and the reason the two are not
    one flag: a manager runs their brands and cannot remove anyone from the
    account.
    """
    org_membership = getattr(request, "org_membership", None)
    return bool(
        org_membership
        and org_membership.org_role
        in (
            OrgMembership.OrgRole.OWNER,
            OrgMembership.OrgRole.ADMIN,
        )
    )


def _granter_role(request):
    """The strongest workspace role the viewer holds anywhere in this org.

    The member list is org-scoped, so there is no single workspace to read the
    viewer's role from, and the role picker has to be drawn from *something*.
    An org owner counts as an owner everywhere, matching `_inviter_workspace_level`.

    Deliberately the strongest and not the weakest: this only decides what the
    picker offers, and `services.set_member_role` re-checks per brand, refusing
    anything they cannot do there. Choosing the weakest would hide options that
    are legitimate in the brand they mean to change.
    """
    org_membership = getattr(request, "org_membership", None)
    if org_membership and org_membership.org_role == OrgMembership.OrgRole.OWNER:
        return WorkspaceMembership.WorkspaceRole.OWNER
    roles = WorkspaceMembership.objects.filter(user=request.user, workspace__organization=request.org).values_list(
        "workspace_role", flat=True
    )
    return max(roles, key=role_rank, default=None)


def _member_row_context(request, membership, *, workspace_ids=None):
    """Context for one re-rendered ``member_row.html``.

    Shared by the three views that redraw a single row after a change, so the
    row cannot come back missing something the list gave it.
    """
    if workspace_ids is None:
        workspace_ids = Workspace.objects.filter(organization=request.org, is_archived=False).values_list(
            "id", flat=True
        )
    ws_memberships = WorkspaceMembership.objects.filter(
        user=membership.user,
        workspace_id__in=list(workspace_ids),
    ).select_related("workspace")

    entry = {
        "membership": membership,
        "user": membership.user,
        "workspace_memberships": list(ws_memberships),
        "role": services.current_member_role(request.org, membership.user),
        # Computed, not assumed. This context used to hardcode `is_admin: True`,
        # which was harmless only while org admins were the only callers — the
        # moment a manager can post here, a hardcoded True would redraw the row
        # asserting powers they do not have.
        "can_manage": can_manage_member(request.user, request.org, membership.user),
        "can_remove": _is_org_admin(request) and membership.user_id != request.user.id,
    }
    _annotate_channel_access(request, [entry])
    return {
        "member": entry,
        "current_user": request.user,
        "workspace_role_choices": assignable_workspace_role_choices(_granter_role(request)),
    }


# The org role (owner/admin/member) is no longer chosen anywhere in the
# interface. It said who administers the account — invite people, create
# workspaces, org settings — and never anything about the content, so shown
# beside the real role it read as one ladder when it was a second axis.
# Everyone invited joins as `member`; administering the account is the owner's.
#
# `require_org_role("admin")` still guards those views and existing admins keep
# working. What is gone is the ability to create another one.


# ---------------------------------------------------------------------------
# Team Members List
# ---------------------------------------------------------------------------


@login_required
@require_org_role("member")
@require_GET
def member_list(request):
    """Show all org members and pending invitations."""
    org = request.org
    # Account administration only: the invite button and the pending list. What
    # someone may do to each *member* is decided per row below, because a
    # manager manages some of these people and not others.
    is_admin = _is_org_admin(request)

    # Active members with their workspace memberships
    memberships = OrgMembership.objects.filter(organization=org).select_related("user").order_by("invited_at")

    org_workspaces = Workspace.objects.filter(organization=org, is_archived=False).order_by("name")
    org_workspace_ids = [ws.id for ws in org_workspaces]

    # Prefetch workspace memberships for all org members
    members_data = []
    user_ids = [m.user_id for m in memberships]
    ws_memberships = WorkspaceMembership.objects.filter(
        user_id__in=user_ids,
        workspace_id__in=org_workspace_ids,
    ).select_related("workspace")
    ws_map = {}
    for wm in ws_memberships:
        ws_map.setdefault(wm.user_id, []).append(wm)

    # Authority is per row, not per page.
    #
    # It used to be one `is_admin` flag off the org role, which hid the actions
    # menu — and the role picker inside it — from every workspace manager. But a
    # single flag cannot be right either way here: the list is org-wide, so a
    # manager legitimately manages some of these people and not others, and the
    # owner's row must be inert for them however senior they are.
    manageable = _manageable_user_ids(request, [m.user for m in memberships])

    for m in memberships:
        held = ws_map.get(m.user_id, [])
        members_data.append(
            {
                "membership": m,
                "user": m.user,
                "workspace_memberships": held,
                # One role per person. Read off the memberships already fetched
                # rather than re-querying: current_member_role would be a query
                # per member on a page that lists all of them.
                "role": max((wm.workspace_role for wm in held), key=role_rank, default=None),
                "can_manage": m.user_id in manageable,
                "can_remove": is_admin and m.user_id != request.user.id,
            }
        )

    # Pending invitations
    pending_invites = []
    if is_admin:
        pending_invites = (
            Invitation.objects.filter(
                organization=org,
                accepted_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .select_related("invited_by")
            .order_by("-created_at")
        )

    _annotate_channel_access(request, members_data)

    # Only what this viewer may hand out: strictly below their own role, so a
    # manager offers Editor down and never another manager.
    granter_role = _granter_role(request)

    return render(
        request,
        "members/list.html",
        {
            "settings_active": "members",
            "members_data": members_data,
            "pending_invites": pending_invites,
            "is_admin": is_admin,
            "org_workspaces": org_workspaces,
            "workspace_role_choices": assignable_workspace_role_choices(granter_role),
            "workspace_role_descriptions": assignable_workspace_role_descriptions(granter_role),
            "current_user": request.user,
        },
    )


# ---------------------------------------------------------------------------
# Invite Member
# ---------------------------------------------------------------------------


@login_required
@require_org_role("admin")
@require_POST
def invite_member(request):
    """Create and send a team member invitation."""
    org = request.org
    email = request.POST.get("email", "").strip()

    # Everyone invited joins as a member of the account. The org role only ever
    # said who administers billing and the team list, never what someone could do
    # with the content, and offering it beside the real role read as one ladder.
    # Administering the account is the owner's job.
    org_role = OrgMembership.OrgRole.MEMBER

    # One role, applied to every brand ticked. It used to be a picker per brand.
    role = request.POST.get("workspace_role", WorkspaceMembership.WorkspaceRole.VIEWER)
    workspace_assignments = []
    org_workspaces = Workspace.objects.filter(organization=org, is_archived=False)
    for ws in org_workspaces:
        if request.POST.get(f"ws_{ws.id}"):
            workspace_assignments.append({"workspace_id": str(ws.id), "role": role})

    # An email that already has an account doesn't need to sign up, so the whole
    # token-and-email round-trip is dead weight — and a mail delivery failure
    # would leave the admin with nothing. Add them on the spot instead.
    existing_user = User.objects.filter(email__iexact=email).first()

    try:
        if existing_user is not None:
            membership = services.add_existing_user_to_org(
                org=org,
                user=existing_user,
                org_role=org_role,
                workspace_assignments=workspace_assignments,
                inviter=request.user,
            )
        else:
            invitation = services.create_invitation(
                org=org,
                email=email,
                org_role=org_role,
                workspace_assignments=workspace_assignments,
                invited_by=request.user,
                inviter=request.user,
            )
    except ValueError as e:
        return HttpResponse(
            f'<div class="text-red-600 text-sm p-3">{e}</div>',
            status=422,
        )

    if request.headers.get("HX-Request"):
        if existing_user is not None:
            # The modal's form targets #invitations-list, but this person is a
            # member now, not a pending invite. Retarget the swap server-side so
            # the row lands in the right list without a page reload.
            response = render(
                request,
                "members/partials/member_row.html",
                _member_row_context(request, membership),
            )
            response["HX-Retarget"] = "#members-list"
            response["HX-Reswap"] = "beforeend"
            return response
        return render(
            request,
            "members/partials/invitation_row.html",
            {
                "invite": invitation,
                "is_admin": True,
            },
        )
    return redirect("members:list")


# ---------------------------------------------------------------------------
# Resend / Revoke Invite
# ---------------------------------------------------------------------------


@login_required
@require_org_role("admin")
@require_POST
def resend_invite(request, invitation_id):
    """Resend an invitation email with a fresh token."""
    invitation = get_object_or_404(Invitation, id=invitation_id, organization=request.org)
    try:
        services.resend_invitation(invitation)
    except ValueError as e:
        return HttpResponse(str(e), status=400)

    if request.headers.get("HX-Request"):
        return render(
            request,
            "members/partials/invitation_row.html",
            {
                "invite": invitation,
                "is_admin": True,
            },
        )
    return redirect("members:list")


@login_required
@require_org_role("admin")
@require_POST
def revoke_invite(request, invitation_id):
    """Revoke a pending invitation."""
    invitation = get_object_or_404(Invitation, id=invitation_id, organization=request.org)
    try:
        services.revoke_invitation(invitation)
    except ValueError as e:
        return HttpResponse(str(e), status=400)

    if request.headers.get("HX-Request"):
        return HttpResponse(status=200, headers={"HX-Trigger": "inviteRevoked"})
    return redirect("members:list")


# ---------------------------------------------------------------------------
# Accept Invite
# ---------------------------------------------------------------------------


def accept_invite(request, token):
    """Accept an invitation (public - no login required for GET)."""
    try:
        invitation = Invitation.objects.select_related("organization", "invited_by").get(token=token)
    except Invitation.DoesNotExist:
        return render(request, "members/invite_expired.html", status=404)

    if invitation.is_expired or invitation.is_accepted:
        return render(request, "members/invite_expired.html")

    # Resolve workspace names for display
    ws_display = []
    for a in invitation.workspace_assignments:
        try:
            ws = Workspace.objects.get(id=a["workspace_id"])
            ws_display.append({"name": ws.name, "role": a.get("role", "viewer")})
        except Workspace.DoesNotExist:
            pass

    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"/accounts/login/?next=/members/invite/{token}/accept/")

        try:
            services.accept_invitation(invitation, request.user)
        except ValueError as e:
            return render(
                request,
                "members/accept_invite.html",
                {
                    "invitation": invitation,
                    "ws_display": ws_display,
                    "error": str(e),
                },
            )

        # Redirect to first assigned workspace or home
        if request.user.last_workspace_id:
            return redirect("calendar:calendar", workspace_id=request.user.last_workspace_id)
        return redirect("/")

    # GET - store token in session for signup flow
    request.session["pending_invite_token"] = token

    return render(
        request,
        "members/accept_invite.html",
        {
            "invitation": invitation,
            "ws_display": ws_display,
            "accept_url": f"/members/invite/{token}/accept/",
        },
    )


# ---------------------------------------------------------------------------
# Update Member Role
# ---------------------------------------------------------------------------


@login_required
@require_org_role("member")
@require_POST
def update_member_role(request, membership_id):
    """Set what this person may do, across every brand they work on.

    One role per person. The role still lives on each ``WorkspaceMembership``
    because that is where the permission checks read it, but the interface stopped
    asking per brand: it was the same answer typed repeatedly, and a role that
    differed between brands by accident was invisible until it bit someone.

    Gated on `can_manage_member`, not on the org role. It used to require org
    admin, which a workspace manager is not — so a manager could not change
    anyone's role even though `services.set_member_role` already implemented
    exactly the rule they should have: strictly below themselves, everywhere the
    person works. The org decorator is kept at `member` only so a non-member of
    the org cannot reach the URL at all.

    The org role (owner/admin/member) is no longer edited here at all. It only
    ever said who administers the account, never what they could do with the
    content, and having two role pickers on one row invited reading them as one
    ladder.
    """
    membership = get_object_or_404(OrgMembership, id=membership_id, organization=request.org)
    if not can_manage_member(request.user, request.org, membership.user):
        raise PermissionDenied("You can only change the role of people ranked below you in the brands you manage.")
    new_role = request.POST.get("workspace_role") or request.POST.get("role")

    try:
        services.set_member_role(request.org, membership.user, new_role, caller=request.user)
    except ValueError as e:
        return HttpResponse(
            f'<div class="text-red-600 text-sm">{e}</div>',
            status=422,
        )

    if request.headers.get("HX-Request"):
        return render(
            request,
            "members/partials/member_row.html",
            _member_row_context(request, membership),
        )
    return redirect("members:list")


# ---------------------------------------------------------------------------
# Remove Member
# ---------------------------------------------------------------------------


@login_required
@require_org_role("admin")
@require_POST
def remove_member(request, membership_id):
    """Remove a member from the organization.

    Still org-admin, unlike the role and brand views next to it. Taking someone
    out of the account entirely is not managing a brand — a manager who no
    longer wants them on their brand unticks it in `manage_workspaces`, which
    leaves the person's account and their work on other brands intact.
    """
    membership = get_object_or_404(OrgMembership, id=membership_id, organization=request.org)

    try:
        services.remove_member(request.org, membership, request.user)
    except ValueError as e:
        return HttpResponse(
            f'<div class="text-red-600 text-sm">{e}</div>',
            status=422,
        )

    if request.headers.get("HX-Request"):
        return HttpResponse(status=200, headers={"HX-Trigger": "memberRemoved"})
    return redirect("members:list")


# ---------------------------------------------------------------------------
# Manage Workspace Assignments
# ---------------------------------------------------------------------------


@login_required
@require_org_role("member")
def manage_workspaces(request, membership_id):
    """Which brands this person works on. GET shows the form, POST saves it.

    Only the ticks. The role is one choice made on the member row and applied
    wherever they are ticked here — this form used to carry a role picker per
    brand, which asked the same question repeatedly and let someone silently end
    up an Editor on one brand and a Viewer on another.

    Open to managers as well as org admins. Safe because
    `services.update_workspace_assignments` checks authority *per brand*: a
    manager who is not a member of a workspace scores zero there, so they can
    neither add someone to it nor untick them out of it.
    """
    membership = get_object_or_404(OrgMembership, id=membership_id, organization=request.org)
    if not can_manage_member(request.user, request.org, membership.user):
        raise PermissionDenied("You can only change the brands of people ranked below you in the brands you manage.")
    org = request.org
    org_workspaces = Workspace.objects.filter(organization=org, is_archived=False).order_by("name")

    # What they already do. Someone being added to their first brand has no role
    # yet, and Viewer is the safe landing: it reads everything and changes
    # nothing, so an accidental tick cannot publish or delete.
    held_role = services.current_member_role(org, membership.user) or WorkspaceMembership.WorkspaceRole.VIEWER

    if request.method == "POST":
        existing = {
            wm.workspace_id: wm.workspace_role
            for wm in WorkspaceMembership.objects.filter(user=membership.user, workspace__organization=org)
        }
        assignments = []
        for ws in org_workspaces:
            ws_key = f"ws_{ws.id}"
            if request.POST.get(ws_key):
                # An existing owner keeps owner: the role is not granted here,
                # so it must not be overwritten here either.
                assignments.append(
                    {
                        "workspace_id": str(ws.id),
                        "role": existing.get(ws.id) if existing.get(ws.id) == "owner" else held_role,
                    }
                )

        try:
            services.update_workspace_assignments(org, membership.user, assignments, inviter=request.user)
        except ValueError as e:
            return HttpResponse(str(e), status=422)

        if request.headers.get("HX-Request"):
            return render(
                request,
                "members/partials/member_row.html",
                _member_row_context(request, membership, workspace_ids=[ws.id for ws in org_workspaces]),
            )
        return redirect("members:list")

    # GET - render assignment form
    current_ws_memberships = WorkspaceMembership.objects.filter(
        user=membership.user,
        workspace_id__in=[ws.id for ws in org_workspaces],
    ).select_related("workspace")
    current_map = {wm.workspace_id: wm for wm in current_ws_memberships}

    workspace_data = []
    for ws in org_workspaces:
        wm = current_map.get(ws.id)
        workspace_data.append(
            {
                "workspace": ws,
                "assigned": wm is not None,
                # Ownership is not granted or revoked here, so the template locks
                # the tick rather than letting it be cleared — unticking would
                # leave the brand with nobody who can hand out access to it.
                "is_owner": wm is not None and wm.workspace_role == WorkspaceMembership.WorkspaceRole.OWNER,
            }
        )

    return render(
        request,
        "members/partials/workspace_assignments.html",
        {
            "membership": membership,
            "workspace_data": workspace_data,
            "current_role": services.current_member_role(org, membership.user),
        },
    )


# ---------------------------------------------------------------------------
# Channel assignments
# ---------------------------------------------------------------------------


@login_required
def manage_channels(request, workspace_id, membership_id):
    """Grant or revoke one member's access to individual channels.

    Workspace-scoped rather than org-scoped like the rest of this file: channel
    assignment only means anything inside one workspace, and the URL carrying
    `workspace_id` is what lets RBACMiddleware resolve the *assigner's* own
    membership, which every check below reads.

    Two separate authorisations, and both matter:
      - can_assign_channels: only owner and manager hand out access at all;
      - can_assign_to: strictly below yourself, so two managers cannot edit
        each other's access in a loop with no adult in the room.

    A manager may only pass on channels they hold themselves, which is why the
    list comes from `assignable_accounts` and not from the workspace.
    """
    from django.core.exceptions import PermissionDenied

    from apps.members.channels import assignable_accounts, can_assign_to

    assigner = request.workspace_membership
    target = get_object_or_404(WorkspaceMembership, id=membership_id, workspace_id=workspace_id)

    if not can_assign_to(assigner, target):
        raise PermissionDenied(
            "You can only assign channels to members below your own role, and only channels you hold yourself."
        )

    saved = False
    channels = list(assignable_accounts(assigner).order_by("platform", "account_name"))
    existing = {a.social_account_id: a for a in ChannelAssignment.objects.filter(membership=target)}

    # Only what this person's role can actually exercise. Offering a Viewer the
    # "Delete comments" box stores a flag the server refuses anyway: the tick
    # saves, does nothing, and whoever ticked it believes it worked.
    available = capabilities_available_to(target)
    available_keys = {key for key, _label, _help in available}
    # The rest are shown apart, ticked-but-inert or plainly unavailable, so a
    # grant made before a demotion does not look like it was silently lost.
    unavailable = [
        (key, label, help_text) for key, label, help_text in CHANNEL_CAPABILITY_LABELS if key not in available_keys
    ]

    if request.method == "POST":
        for channel in channels:
            checked = request.POST.get(f"channel_{channel.id}")
            assignment = existing.get(channel.id)
            if not checked:
                # Unticking the channel removes the row entirely. The row
                # existing *is* the view grant, so keeping it with every flag
                # off would leave the member still seeing the channel.
                if assignment:
                    assignment.delete()
                continue
            if assignment is None:
                assignment = ChannelAssignment(membership=target, social_account=channel, assigned_by=request.user)
            for capability in CHANNEL_CAPABILITIES:
                # Capabilities their role cannot use are not on the form, so
                # reading them from POST would clear a grant made while they held
                # a higher role. Left untouched: the role check keeps it inert
                # meanwhile, and restoring the role restores what they had.
                if capability not in available_keys:
                    continue
                setattr(assignment, capability, bool(request.POST.get(f"{capability}_{channel.id}")))
            assignment.full_clean()
            assignment.save()

        existing = {a.social_account_id: a for a in ChannelAssignment.objects.filter(membership=target)}
        # Falls through to re-render the form below.
        #
        # It used to return a 204 with an HX-Trigger event — called with the
        # wrong signature, so saving raised TypeError and 500'd, and even fixed
        # it would have shown nothing: no surface in this app listens for that
        # event. Re-rendering shows the boxes in their saved state, which is
        # feedback that cannot get out of step with what was stored.
        saved = True

    rows = []
    for channel in channels:
        assignment = existing.get(channel.id)
        rows.append(
            {
                "channel": channel,
                "assigned": assignment is not None,
                "capabilities": [
                    (key, label, help_text, bool(assignment and getattr(assignment, key)))
                    for key, label, help_text in available
                ],
                "blocked": [
                    (key, label, help_text, bool(assignment and getattr(assignment, key)))
                    for key, label, help_text in unavailable
                ],
            }
        )

    return render(
        request,
        "members/partials/channel_assignments.html",
        {
            "workspace_id": workspace_id,
            "target": target,
            "rows": rows,
            "saved": saved,
            "target_role": target.get_workspace_role_display(),
        },
    )
