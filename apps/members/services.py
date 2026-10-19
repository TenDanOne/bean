"""Team member invite and management services."""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.workspaces.models import Workspace

from .models import Invitation, OrgMembership, WorkspaceMembership, role_rank

logger = logging.getLogger(__name__)

INVITE_EXPIRY_DAYS = 7

# The org hierarchy is its own scale (owner/admin/member) and has no workspace
# equivalent, so it stays here.
ORG_ROLE_LEVEL = {
    OrgMembership.OrgRole.OWNER: 3,
    OrgMembership.OrgRole.ADMIN: 2,
    OrgMembership.OrgRole.MEMBER: 1,
}

# The workspace hierarchy comes from models.role_rank. There used to be a local
# WS_ROLE_LEVEL copy here — a third one, after WORKSPACE_ROLE_ORDER and the one
# in decorators.py — and it never received `reviewer` when that role was added.
# Since an unknown role scores 0 and 0 is rejected below, assigning Reviewer
# failed with "Unknown workspace role" even though the picker offered it, and an
# existing reviewer counted as having no authority at all.


def _ws_level(role) -> int:
    """Rank of a workspace role, 0 for anything not recognised."""
    return role_rank(role)


def _reject_workspace_owner(role, *, current_role=None):
    """Refuse to *grant* workspace ownership. Owner is set at creation only.

    The org level has always refused this ("Cannot invite someone as an
    organization owner"), but the workspace level did not, and a workspace owner
    sees every channel in the workspace **without any assignment** — so handing
    the role out is the one move that defeats the whole channel-assignment layer
    in a single click. There is no legitimate reason to need two owners on a
    brand; use ownership transfer, which moves the role instead of duplicating
    it.

    ``current_role`` is what the membership holds today. Someone who is already
    the owner may be resubmitted as owner, because this also runs on the "save
    the whole form" path: refusing outright would mean that opening an existing
    owner's workspace form and pressing save silently demoted them — a
    destructive edit nobody asked for, dressed up as a security check.
    """
    if role != WorkspaceMembership.WorkspaceRole.OWNER:
        return
    if current_role == WorkspaceMembership.WorkspaceRole.OWNER:
        return
    raise ValueError("No se puede otorgar la propiedad del espacio de trabajo. Transfiere la propiedad.")


def _inviter_org_level(inviter, org):
    membership = OrgMembership.objects.filter(user=inviter, organization=org).first()
    if not membership:
        return 0
    return ORG_ROLE_LEVEL.get(membership.org_role, 0)


def _inviter_workspace_level(inviter, org, workspace_id):
    """Return inviter's effective workspace role level in *workspace_id*.

    Org owners are treated as workspace owners across every workspace in their
    org (matches the spirit of `@require_org_role("owner")` gating org-wide
    actions). Org admins are bounded by their actual workspace membership; if
    they aren't a member, they have zero authority on that workspace.
    """
    if _inviter_org_level(inviter, org) >= ORG_ROLE_LEVEL[OrgMembership.OrgRole.OWNER]:
        return _ws_level(WorkspaceMembership.WorkspaceRole.OWNER)
    ws_membership = WorkspaceMembership.objects.filter(user=inviter, workspace_id=workspace_id).first()
    if not ws_membership:
        return 0
    return _ws_level(ws_membership.workspace_role)


def _validate_invite_request(org, org_role, workspace_assignments, inviter):
    """Permission checks shared by every path that grants org/workspace access.

    Both ``create_invitation`` (email flow) and ``add_existing_user_to_org``
    (direct add) route through here. Keeping it in one place is the point: a
    second grant path that skipped these checks would be a privilege-escalation
    hole, letting an admin clone their own tier.

    Raises:
        ValueError: On any role the inviter isn't entitled to grant, an unknown
            role, or a workspace outside ``org``.
    """
    # Don't allow inviting as owner (use ownership transfer instead).
    if org_role == OrgMembership.OrgRole.OWNER:
        raise ValueError("No se puede invitar a alguien como propietario de la organización.")

    # Enforce org-role hierarchy: inviter cannot grant a role above their own.
    inviter_org_level = _inviter_org_level(inviter, org)
    requested_org_level = ORG_ROLE_LEVEL.get(org_role, 0)
    if requested_org_level == 0:
        raise ValueError(f"Rol de organización desconocido: {org_role!r}.")
    # Strict inequality on org-role, but ONLY for the admin/owner tier:
    # blocks lateral admin grants (a compromised admin shouldn't be able to
    # clone their privileges) while still permitting member-tier invites —
    # which the workspace-manager → client-invite flow relies on, since
    # those managers are themselves only org-role=member.
    admin_level = ORG_ROLE_LEVEL[OrgMembership.OrgRole.ADMIN]
    if requested_org_level >= admin_level and requested_org_level >= inviter_org_level:
        raise ValueError("Solo los propietarios de la organización pueden invitar a alguien como administrador.")

    # Validate workspace assignments belong to org AND don't exceed inviter's
    # workspace role in that specific workspace.
    org_workspace_ids = set(Workspace.objects.filter(organization=org, is_archived=False).values_list("id", flat=True))
    for assignment in workspace_assignments:
        ws_id = uuid.UUID(str(assignment["workspace_id"]))
        if ws_id not in org_workspace_ids:
            raise ValueError(f"El espacio de trabajo {ws_id} no pertenece a esta organización.")

        requested_ws_role = assignment.get("role", WorkspaceMembership.WorkspaceRole.VIEWER)
        _reject_workspace_owner(requested_ws_role)
        requested_ws_level = _ws_level(requested_ws_role)
        if requested_ws_level == 0:
            raise ValueError(f"Rol de espacio de trabajo desconocido: {requested_ws_role!r}.")
        inviter_ws_level = _inviter_workspace_level(inviter, org, ws_id)
        if requested_ws_level > inviter_ws_level:
            raise ValueError("No puedes otorgar un rol superior al tuyo en ese espacio de trabajo.")


def create_invitation(org, email, org_role, workspace_assignments, invited_by, *, inviter=None):
    """Create an invitation and send the invite email.

    Args:
        org: Organization to invite into.
        email: Invitee email address.
        org_role: "member" or "admin".
        workspace_assignments: list of {"workspace_id": "...", "role": "..."}.
        invited_by: User who is sending the invite.

    Returns:
        The created Invitation.

    Raises:
        ValueError: If the email already belongs to a member or has a pending invite.
    """
    email = email.strip().lower()

    # Check if already a member
    if OrgMembership.objects.filter(organization=org, user__email=email).exists():
        raise ValueError("Esta persona ya es miembro de tu organización.")

    # Check for pending invite
    pending = Invitation.objects.filter(
        organization=org,
        email=email,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if pending:
        raise ValueError("Ya hay una invitación pendiente para este correo. Puedes reenviarla.")

    # Default `inviter` to `invited_by` so legacy callers without the kwarg
    # still get an enforced check (no silent bypass).
    _validate_invite_request(org, org_role, workspace_assignments, inviter or invited_by)

    invitation = Invitation.objects.create(
        organization=org,
        email=email,
        org_role=org_role,
        workspace_assignments=workspace_assignments,
        invited_by=invited_by,
        expires_at=timezone.now() + timedelta(days=INVITE_EXPIRY_DAYS),
    )

    _send_invite_email(invitation)
    return invitation


def absorb_other_org_memberships(user, keep_org_id):
    """Drop every OrgMembership ``user`` holds outside ``keep_org_id``, deleting those orgs.

    v1 assumes one organization per user — ``RBACMiddleware`` resolves the active
    org with a single ``.first()``. A user holding two memberships would land in
    one org or the other unpredictably between requests, so any path that grants
    membership must collapse the rest.

    **This is destructive and unconditional by design.** Signup auto-provisions a
    personal "My Organization" for everyone ([apps.accounts.signals]), so in
    practice the org being removed is that empty placeholder — but if the user had
    actually worked in it, their workspaces, connected accounts, posts and media
    go with it via cascade. That trade-off was chosen deliberately over blocking
    the invite. The warning log below is the only record left afterwards.
    """
    from apps.organizations.models import Organization

    doomed = OrgMembership.objects.filter(user=user).exclude(organization_id=keep_org_id).select_related("organization")
    for membership in doomed:
        org = membership.organization
        workspaces = Workspace.objects.filter(organization=org)
        logger.warning(
            "Absorbing %s into org %s: deleting org %s (%s) with %d workspace(s). "
            "Any connected accounts, posts and media in it are destroyed.",
            user.email,
            keep_org_id,
            org.id,
            org.name,
            workspaces.count(),
        )
        membership.delete()
        workspaces.delete()
        Organization.objects.filter(pk=org.pk).delete()


def add_existing_user_to_org(org, user, org_role, workspace_assignments, *, inviter):
    """Grant an already-registered user membership immediately — no email round-trip.

    The email invite flow is useless when the address already has an account: the
    person doesn't need to sign up, and a delivery failure (or a console-only mail
    backend) leaves the admin with nothing. This adds them on the spot instead.

    Applies exactly the same permission checks as ``create_invitation`` — see
    ``_validate_invite_request``.

    Returns:
        The created OrgMembership.

    Raises:
        ValueError: If the user already belongs to ``org``, or the inviter isn't
            entitled to grant the requested roles.
    """
    if OrgMembership.objects.filter(organization=org, user=user).exists():
        raise ValueError("Esta persona ya es miembro de tu organización.")

    _validate_invite_request(org, org_role, workspace_assignments, inviter)

    org_membership = OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=org_role,
        accepted_at=timezone.now(),
    )

    for assignment in workspace_assignments:
        ws_id = uuid.UUID(str(assignment["workspace_id"]))
        role = assignment.get("role", WorkspaceMembership.WorkspaceRole.VIEWER)
        WorkspaceMembership.objects.update_or_create(
            user=user,
            workspace_id=ws_id,
            defaults={"workspace_role": role},
        )

    absorb_other_org_memberships(user, org.id)

    # Point their dashboard at the org they just joined; their previous
    # last_workspace_id belonged to an org that no longer exists.
    if workspace_assignments:
        user.last_workspace_id = uuid.UUID(str(workspace_assignments[0]["workspace_id"]))
        user.save(update_fields=["last_workspace_id"])

    _notify_added_to_org(user, org, inviter)
    return org_membership


def _notify_added_to_org(user, org, inviter):
    """In-app notice that someone added this user to an organization.

    The direct-add path sends no email, so this is the only signal the user gets.
    Never let a notification failure roll back a completed membership.
    """
    try:
        from apps.notifications.engine import notify
        from apps.notifications.models import EventType

        notify(
            user=user,
            event_type=EventType.TEAM_MEMBER_INVITED,
            title=f"Te agregaron a {org.name}",
            body=f"{getattr(inviter, 'display_name', None) or 'An administrator'} added you to {org.name}.",
            data={"organization_id": str(org.id)},
        )
    except Exception:
        logger.exception("Failed to notify %s about being added to org %s", user.email, org.id)


def accept_invitation(invitation, user, *, require_email_match=True):
    """Accept an invitation: create org + workspace memberships.

    Args:
        invitation: The Invitation to accept.
        user: The User accepting.
        require_email_match: When True (default), reject if the user's email
            differs from the invitation's. The signup signal path passes
            False because the session-bound token is itself proof of email
            delivery (and social logins return whatever email the provider
            owns, which often differs from the invited address).

    Raises:
        ValueError: If the invitation is expired, already accepted, or the
            user's email does not match (when require_email_match is True).
    """
    if invitation.is_expired:
        raise ValueError("Esta invitación expiró.")
    if invitation.is_accepted:
        raise ValueError("Esta invitación ya fue aceptada.")

    if require_email_match and user.email.lower() != invitation.email.lower():
        raise ValueError("Esta invitación se envió a otro correo electrónico.")

    # Create org membership (skip if exists, e.g. user was already added)
    org_membership, created = OrgMembership.objects.get_or_create(
        user=user,
        organization=invitation.organization,
        defaults={"org_role": invitation.org_role},
    )

    # Create workspace memberships
    for assignment in invitation.workspace_assignments:
        import uuid as uuid_mod

        ws_id = uuid_mod.UUID(str(assignment["workspace_id"]))
        role = assignment.get("role", WorkspaceMembership.WorkspaceRole.VIEWER)
        WorkspaceMembership.objects.get_or_create(
            user=user,
            workspace_id=ws_id,
            defaults={"workspace_role": role},
        )

    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])

    # Collapse any other org the user belonged to. Signup-through-invite used to
    # do this itself, which left a hole: an ALREADY-REGISTERED user accepting an
    # invite kept their own org and ended up in two, making RBACMiddleware's
    # single-org `.first()` lookup nondeterministic for them.
    absorb_other_org_memberships(user, invitation.organization_id)

    # Set last workspace for dashboard redirect
    if invitation.workspace_assignments:
        first_ws_id = uuid.UUID(str(invitation.workspace_assignments[0]["workspace_id"]))
        user.last_workspace_id = first_ws_id
        user.save(update_fields=["last_workspace_id"])

    return org_membership


def resend_invitation(invitation):
    """Resend an invitation with a fresh token and expiry.

    Raises:
        ValueError: If the invitation is already accepted.
    """
    if invitation.is_accepted:
        raise ValueError("Esta invitación ya fue aceptada.")

    import secrets

    invitation.token = secrets.token_urlsafe(32)
    invitation.expires_at = timezone.now() + timedelta(days=INVITE_EXPIRY_DAYS)
    invitation.save(update_fields=["token", "expires_at"])

    _send_invite_email(invitation)
    return invitation


def revoke_invitation(invitation):
    """Revoke an invitation by expiring it immediately."""
    if invitation.is_accepted:
        raise ValueError("No se puede revocar una invitación ya aceptada.")
    invitation.expires_at = timezone.now()
    invitation.save(update_fields=["expires_at"])


def remove_member(org, membership, removed_by):
    """Remove a member from the organization and all its workspaces.

    Args:
        org: Organization.
        membership: The OrgMembership to remove.
        removed_by: User performing the removal.

    Raises:
        ValueError: If trying to remove the last owner or yourself.
    """
    if membership.user_id == removed_by.id:
        raise ValueError("No puedes quitarte a ti mismo de la organización.")

    if membership.org_role == OrgMembership.OrgRole.OWNER:
        owner_count = OrgMembership.objects.filter(organization=org, org_role=OrgMembership.OrgRole.OWNER).count()
        if owner_count <= 1:
            raise ValueError("No se puede quitar al último propietario de la organización.")

    # Delete workspace memberships in this org's workspaces
    org_workspace_ids = Workspace.objects.filter(organization=org).values_list("id", flat=True)
    WorkspaceMembership.objects.filter(
        user=membership.user,
        workspace_id__in=org_workspace_ids,
    ).delete()

    membership.delete()


def update_member_org_role(org, membership, new_role, *, caller=None):
    """Update a member's organization role.

    Raises:
        ValueError: If demoting the last owner, or if `caller` lacks authority
            to either remove the existing role or set the requested one.
    """
    if new_role == OrgMembership.OrgRole.OWNER:
        raise ValueError("No se puede promover a propietario. Transfiere la propiedad.")

    new_level = ORG_ROLE_LEVEL.get(new_role, 0)
    if new_level == 0:
        raise ValueError(f"Rol de organización desconocido: {new_role!r}.")

    # Caller hierarchy: must be at least as senior as both the existing role
    # and the requested role. Blocks an admin demoting an owner (existing
    # role outranks them) or escalating someone above their own tier.
    if caller is not None:
        caller_level = _inviter_org_level(caller, org)
        existing_level = ORG_ROLE_LEVEL.get(membership.org_role, 0)
        if caller_level < existing_level:
            raise ValueError("No puedes cambiar a un miembro con un rol superior al tuyo.")
        admin_level = ORG_ROLE_LEVEL[OrgMembership.OrgRole.ADMIN]
        if new_level >= admin_level and new_level >= caller_level:
            raise ValueError("Solo los propietarios de la organización pueden promover a alguien a administrador.")

    if membership.org_role == OrgMembership.OrgRole.OWNER:
        owner_count = OrgMembership.objects.filter(organization=org, org_role=OrgMembership.OrgRole.OWNER).count()
        if owner_count <= 1:
            raise ValueError("No se puede cambiar el rol del último propietario de la organización.")

    membership.org_role = new_role
    membership.save(update_fields=["org_role"])
    return membership


def set_member_role(org, user, new_role, *, caller=None):
    """Set one person's role across every workspace they belong to in ``org``.

    One role per person, not one per brand. The model still stores the role on
    each ``WorkspaceMembership`` — that is where every permission check reads it
    from, and changing that would touch 95 call sites for no gain — but the
    interface no longer asks the question twice. Picking "Editor" for someone
    meant picking it again for every brand, and a role that differed between
    brands by accident was invisible until it bit.

    Workspace ownership is left alone: it is not granted here (see
    ``_reject_workspace_owner``), so it must not be overwritten here either.

    Returns the number of memberships changed.
    """
    _reject_workspace_owner(new_role)
    new_level = _ws_level(new_role)
    if new_level == 0:
        raise ValueError(f"Rol de espacio de trabajo desconocido: {new_role!r}.")

    memberships = list(
        WorkspaceMembership.objects.filter(user=user, workspace__organization=org).exclude(
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
    )

    # Checked per brand, not once against a summary of the caller's standing:
    # an owner of one brand and an editor of another may only change roles in the
    # first. Both ends matter — the role being granted and the role being taken
    # away — or a manager could demote someone above them and then grant
    # themselves the difference.
    if caller is not None:
        for membership in memberships:
            caller_level = _inviter_workspace_level(caller, org, membership.workspace_id)
            if caller_level < _ws_level(WorkspaceMembership.WorkspaceRole.MANAGER):
                raise ValueError("No tienes permiso para cambiar roles.")
            if new_level >= caller_level:
                raise ValueError("Solo puedes dar un rol inferior al tuyo.")
            if _ws_level(membership.workspace_role) >= caller_level:
                raise ValueError("No puedes cambiar el rol de alguien con un rol igual o superior al tuyo.")

    changed = 0
    for membership in memberships:
        if membership.workspace_role == new_role:
            continue
        membership.workspace_role = new_role
        membership.save(update_fields=["workspace_role"])
        changed += 1
    return changed


def current_member_role(org, user):
    """The role this person holds, or None if they are in no workspace yet.

    They can in principle hold different roles in different brands — older rows,
    or a direct database edit — so this reports the strongest one rather than
    picking arbitrarily. Under-reporting would show someone as a Viewer while
    they were editing.
    """
    roles = list(
        WorkspaceMembership.objects.filter(user=user, workspace__organization=org).values_list(
            "workspace_role", flat=True
        )
    )
    if not roles:
        return None
    return max(roles, key=_ws_level)


def update_workspace_assignments(org, user, assignments, *, inviter=None):
    """Update workspace assignments for a member.

    Args:
        org: Organization.
        user: The user whose assignments to update.
        assignments: list of {"workspace_id": "...", "role": "..."}.
        inviter: The user performing the change (for role-hierarchy enforcement).
            When None, no caller-level check is applied (used only by tests or
            internal admin scripts).
    """
    import uuid as uuid_mod

    org_workspace_ids = set(Workspace.objects.filter(organization=org, is_archived=False).values_list("id", flat=True))

    # What they hold today, so an existing owner can be resubmitted as owner
    # without the guard below reading it as a promotion.
    held_roles = dict(
        WorkspaceMembership.objects.filter(user=user, workspace_id__in=org_workspace_ids).values_list(
            "workspace_id", "workspace_role"
        )
    )

    desired = {}
    for a in assignments:
        ws_id = uuid_mod.UUID(str(a["workspace_id"]))
        if ws_id not in org_workspace_ids:
            raise ValueError(f"El espacio de trabajo {ws_id} no pertenece a esta organización.")
        requested_role = a.get("role", WorkspaceMembership.WorkspaceRole.VIEWER)
        _reject_workspace_owner(requested_role, current_role=held_roles.get(ws_id))
        requested_level = _ws_level(requested_role)
        if requested_level == 0:
            raise ValueError(f"Rol de espacio de trabajo desconocido: {requested_role!r}.")
        # Preserving is not granting. The form that calls this decides which
        # brands someone works on and resubmits the role they already hold, so
        # checking the height of an unchanged role would refuse the whole save —
        # a caller below them could not tick a different brand for that person.
        # A *change* upward still goes through the check below.
        if inviter is not None and requested_role != held_roles.get(ws_id):
            inviter_level = _inviter_workspace_level(inviter, org, ws_id)
            if requested_level > inviter_level:
                raise ValueError("No puedes otorgar un rol superior al tuyo en ese espacio de trabajo.")
        desired[ws_id] = requested_role

    # Current assignments in this org
    current = WorkspaceMembership.objects.filter(
        user=user,
        workspace_id__in=org_workspace_ids,
    )
    current_map = {m.workspace_id: m for m in current}

    # The inviter must also have authority over the *existing* role. Without
    # this check, a viewer-level admin could silently downgrade an owner by
    # submitting the form with role="viewer" (request_level <= inviter_level
    # passes the earlier check; the actual demotion of the owner row is the
    # privilege violation). Apply to both removal and role-change paths.
    if inviter is not None:
        for ws_id, m in current_map.items():
            existing_level = _ws_level(m.workspace_role)
            inviter_level = _inviter_workspace_level(inviter, org, ws_id)
            if ws_id not in desired:
                # About to delete this membership entirely.
                if existing_level > inviter_level:
                    raise ValueError("No puedes quitar una membresía cuyo rol actual es superior al tuyo.")
            elif desired[ws_id] != m.workspace_role and existing_level > inviter_level:
                # About to change this membership's role.
                raise ValueError("No puedes modificar una membresía cuyo rol actual es superior al tuyo.")

    # Remove memberships not in desired
    for ws_id, m in current_map.items():
        if ws_id not in desired:
            m.delete()

    # Create or update
    for ws_id, role in desired.items():
        if ws_id in current_map:
            m = current_map[ws_id]
            if m.workspace_role != role:
                m.workspace_role = role
                m.save(update_fields=["workspace_role"])
        else:
            WorkspaceMembership.objects.create(
                user=user,
                workspace_id=ws_id,
                workspace_role=role,
            )


def _send_invite_email(invitation):
    """Send the invite email for an invitation."""
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    accept_url = f"{app_url}/members/invite/{invitation.token}/accept/"

    context = {
        "invitation": invitation,
        "accept_url": accept_url,
        "org_name": invitation.organization.name,
        "invited_by": invitation.invited_by,
        "app_url": app_url,
    }

    subject = f"Te invitaron a unirte a {invitation.organization.name} en BEAN"
    text_content = render_to_string("members/email/invite.txt", context)
    html_content = render_to_string("members/email/invite.html", context)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        to=[invitation.email],
    )
    msg.attach_alternative(html_content, "text/html")

    try:
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send invite email to %s", invitation.email)
