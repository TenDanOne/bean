"""Resolving what a member may see and do on each connected channel.

Two questions, asked constantly:

    visible_accounts(membership)          -> which channels does this person see
    can_on_channel(membership, acc, cap)  -> may they do X on this one

Everything that filters by channel goes through here rather than writing its own
query, because the interesting rule is not obvious from a queryset: **only an
owner sees every channel without being assigned it.** Everyone else — manager
included — sees exactly what they were given.

That is deliberate. It is what lets the owner of a personal account be a
reviewer with one assigned channel and see nothing else in the workspace, and it
means adding a member is safe by default: they arrive seeing nothing.

The layer is orthogonal to the permission keys in models.py, and both must pass.
Replying to a comment needs `reply_from_inbox` from the role *and* `can_reply`
on that channel. Neither check changes what the other means.
"""

from __future__ import annotations

from apps.members.models import (
    CHANNEL_CAPABILITIES,
    ChannelAssignment,
    capabilities_available_to,
    role_rank,
)

# Roles that see the whole workspace without any assignment. Just the one.
UNRESTRICTED_ROLES = frozenset({"owner"})


def sees_all_channels(membership) -> bool:
    """True when this member needs no assignment to see a channel."""
    if membership is None:
        return False
    return membership.workspace_role in UNRESTRICTED_ROLES


def assignment_map(membership) -> dict:
    """``{social_account_id: ChannelAssignment}`` for this member.

    One query. Callers checking several channels in a loop should use this
    rather than calling ``can_on_channel`` per channel, which would not.
    """
    if membership is None:
        return {}
    return {a.social_account_id: a for a in ChannelAssignment.objects.filter(membership=membership)}


def visible_accounts(membership, queryset=None, capability: str | None = None):
    """Narrow a ``SocialAccount`` queryset to what this member may see.

    Pass a queryset that is already workspace-scoped; this only removes rows,
    it never widens the set. Without a membership it returns nothing, which is
    the right answer for an anonymous or non-member request.

    ``capability`` narrows further to channels where that flag is granted.
    Analytics passes ``can_view_metrics``: seeing a channel's comments and
    seeing its numbers are separate grants on purpose, because the person who
    answers the inbox is often not the person who should read the reach.
    """
    from apps.social_accounts.models import SocialAccount

    if capability is not None and capability not in CHANNEL_CAPABILITIES:
        raise ValueError(f"Unknown channel capability {capability!r}. Expected one of {CHANNEL_CAPABILITIES}.")
    if queryset is None:
        queryset = (
            SocialAccount.objects.none()
            if membership is None
            else SocialAccount.objects.for_workspace(membership.workspace_id)
        )
    if membership is None:
        return queryset.none()
    if sees_all_channels(membership):
        return queryset
    if capability is not None and capability not in {
        key for key, _label, _help in capabilities_available_to(membership)
    }:
        # Their role cannot do this anywhere, so no channel qualifies. Reading
        # the stored flag alone would keep listing channels granted under a role
        # they have since been moved off.
        return queryset.none()
    filters = {"assignments__membership": membership}
    if capability is not None:
        filters[f"assignments__{capability}"] = True
    return queryset.filter(**filters)


def can_on_channel(membership, social_account, capability: str) -> bool:
    """May this member do ``capability`` on this channel?

    ``capability`` is one of CHANNEL_CAPABILITIES. Three things must hold: the
    channel is in their workspace, their **role** can do this at all, and they
    were granted the flag on this channel. An owner satisfies the last two by
    definition.

    The role requirement lives here rather than only in the dialog that hands
    out the boxes. A stored flag outlives the role it was granted under — demote
    a manager to editor and the `can_delete_comments` row is still True — so a
    check that read the flag alone let a demotion leave a permanent action
    behind. The dialog stops offering it; this stops honouring it.
    """
    if capability not in CHANNEL_CAPABILITIES:
        raise ValueError(f"Unknown channel capability {capability!r}. Expected one of {CHANNEL_CAPABILITIES}.")
    if membership is None or social_account is None:
        return False
    # An owner of a *different* workspace is not an owner here.
    if social_account.workspace_id != membership.workspace_id:
        return False
    if sees_all_channels(membership):
        return True
    if capability not in {key for key, _label, _help in capabilities_available_to(membership)}:
        return False
    assignment = ChannelAssignment.objects.filter(membership=membership, social_account=social_account).first()
    return bool(assignment and getattr(assignment, capability))


def can_assign_channels(membership) -> bool:
    """May this member hand out channel assignments at all?

    Owner and manager. A manager is bounded twice over: only channels they hold
    themselves (``assignable_accounts``) and only to people ranked strictly
    below them (``can_assign_to``), so delegating cannot manufacture access the
    delegator never had, and two managers cannot edit each other in a loop.
    """
    if membership is None:
        return False
    return role_rank(membership.workspace_role) >= role_rank("manager")


def assignable_accounts(membership):
    """Channels this member is allowed to hand to someone else.

    Everything they can see — the whole workspace for an owner, and for a
    manager exactly what they were assigned. Passing on what you do not hold is
    how delegation quietly becomes escalation.
    """
    if not can_assign_channels(membership):
        from apps.social_accounts.models import SocialAccount

        return SocialAccount.objects.none()
    return visible_accounts(membership)


def can_manage_member(viewer_user, org, target_user) -> bool:
    """May ``viewer_user`` change what ``target_user`` can do in this org?

    Answers the question the team page asks per row. Org admins and owners can
    manage anyone below them; a workspace manager can manage people who rank
    strictly below them — and only where they hold that authority in **every**
    brand the target works on, because the role applies across all of them.

    An owner is never manageable by a manager: they rank above, so the strict
    comparison refuses. That is also what keeps the owner's channel dialog out
    of a manager's reach — the owner is the one handing access out in the first
    place, and a subordinate editing that would invert the whole arrangement.

    Deliberately derived from the *target's* memberships rather than from
    ``request.workspace_membership``. The team page has no workspace in its URL,
    so that attribute is whatever brand the viewer last visited — possibly one
    the target is not even in, and possibly None.
    """
    from apps.members.models import OrgMembership, WorkspaceMembership

    if viewer_user is None or target_user is None or viewer_user.id == target_user.id:
        return False

    viewer_org = OrgMembership.objects.filter(user=viewer_user, organization=org).first()
    if viewer_org is None:
        return False
    if viewer_org.org_role in (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN):
        return True

    target_roles = WorkspaceMembership.objects.filter(user=target_user, workspace__organization=org).values_list(
        "workspace_id", "workspace_role"
    )
    target_roles = list(target_roles)
    if not target_roles:
        # Nobody to outrank yet. Adding them to a brand is a different action,
        # gated where that happens.
        return False

    viewer_roles = dict(
        WorkspaceMembership.objects.filter(user=viewer_user, workspace__organization=org).values_list(
            "workspace_id", "workspace_role"
        )
    )
    for workspace_id, target_role in target_roles:
        viewer_role = viewer_roles.get(workspace_id)
        if viewer_role is None:
            return False
        if role_rank(viewer_role) < role_rank("manager"):
            return False
        if role_rank(target_role) >= role_rank(viewer_role):
            return False
    return True


def can_assign_to(assigner, target) -> bool:
    """May ``assigner`` change ``target``'s channel assignments?

    Strictly below themselves, and inside the same workspace. Equal ranks are
    refused on purpose: two managers editing each other's access is a loop with
    no adult in the room.
    """
    if assigner is None or target is None:
        return False
    if assigner.workspace_id != target.workspace_id:
        return False
    if not can_assign_channels(assigner):
        return False
    return role_rank(assigner.workspace_role) > role_rank(target.workspace_role)
