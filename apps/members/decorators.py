"""Permission decorators for RBAC enforcement."""

import functools

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def require_org_role(min_role):
    """Decorator that requires a minimum org role.

    Role hierarchy: owner > admin > member
    """
    role_hierarchy = {"owner": 3, "admin": 2, "member": 1}

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.org_membership:
                raise PermissionDenied("You are not a member of any organization.")
            # Org roles are their own scale (owner/admin/member) — deliberately
            # not the workspace one, which knows nothing about "admin".
            user_level = role_hierarchy.get(request.org_membership.org_role, 0)
            required_level = role_hierarchy.get(min_role, 0)
            if user_level < required_level:
                raise PermissionDenied("Insufficient organization role.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_workspace_role(min_role):
    """Decorator that requires a minimum workspace role.

    For users with custom roles, this checks the custom role's permissions
    against the built-in role's permissions to determine equivalence.
    Users with custom roles are treated as having the permissions defined
    in their custom role, so this decorator falls back to require_permission
    for the key permissions of the minimum role.

    Role hierarchy (built-in): owner > manager > editor > reviewer > contributor
    > client > viewer. See WORKSPACE_ROLE_ORDER in members/models.py.
    """
    # The order lives in members/models.py so channel assignment can share it —
    # two copies of a role hierarchy is one copy too many.
    from apps.members.models import role_rank

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.workspace_membership:
                raise PermissionDenied("You are not a member of this workspace.")
            membership = request.workspace_membership
            user_level = role_rank(membership.workspace_role)
            required_level = role_rank(min_role)
            if user_level < required_level:
                raise PermissionDenied("Insufficient workspace role.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_permission(permission_key):
    """Decorator that checks a specific permission key against effective permissions.

    This works with both built-in roles and custom roles via the
    effective_permissions property on WorkspaceMembership.
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.workspace_membership:
                raise PermissionDenied("You are not a member of this workspace.")
            perms = request.workspace_membership.effective_permissions
            if not perms.get(permission_key, False):
                raise PermissionDenied(f"Permission denied: {permission_key}")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_channel_capability(capability, resolver):
    """Gate a view on a per-channel capability, on top of the role check.

    The two layers are orthogonal and **both** must pass: replying needs
    `reply_from_inbox` from the role *and* `can_reply` on that specific channel.
    Stack this under `@require_permission(...)`, never instead of it — this only
    answers "on this channel", not "at all".

    ``resolver`` receives ``(request, *args, **kwargs)`` and returns the
    ``SocialAccount`` being acted on, or None. A view whose object cannot be
    resolved is refused rather than allowed: a channel we cannot identify is not
    a channel we can prove they were given.

    Written as a decorator because the alternative — an `if` inside each view —
    is how three of these got written and none of them got applied. It reads the
    same helpers as `visible_accounts`, so the list someone sees and the actions
    they can take cannot disagree.
    """
    from apps.members.channels import can_on_channel

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            membership = getattr(request, "workspace_membership", None)
            if not membership:
                raise PermissionDenied("You are not a member of this workspace.")
            account = resolver(request, *args, **kwargs)
            if account is None:
                raise PermissionDenied("This action has no channel to check against.")
            if not can_on_channel(membership, account, capability):
                raise PermissionDenied(
                    f"You do not have permission to do this on {account.account_name or account.account_handle}."
                )
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_org_permission(permission_key):
    """Decorator that gates a view on an org-level permission key.

    Unlike ``require_workspace_role`` / ``require_permission`` (workspace-
    scoped via the RBAC middleware), this decorator resolves the
    ``OrgMembership`` for ``(request.user, URL <org_id>)`` directly — no
    middleware fallback to ``last_workspace_id``. The view must accept
    ``org_id`` as a URL kwarg (typed ``<uuid:org_id>``).

    Composes ``@login_required`` internally so the view file doesn't
    need to remember to stack it. Anonymous → login redirect; non-member
    of the URL org → 403; member but lacking the permission → 403.

    On success, attaches ``request.org_membership`` and ``request.org``
    so the wrapped view can use them without re-querying.
    """
    from apps.members.models import OrgMembership, has_org_permission

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            org_id = kwargs.get("org_id")
            if org_id is None:
                raise PermissionDenied("URL is missing required org_id.")
            try:
                membership = OrgMembership.objects.select_related("organization").get(
                    user=request.user, organization_id=org_id
                )
            except OrgMembership.DoesNotExist as exc:
                raise PermissionDenied("Not a member of this organization.") from exc

            if not has_org_permission(membership, permission_key):
                raise PermissionDenied(f"Missing org permission: {permission_key}")

            # Make resolved context available to the view body.
            request.org_membership = membership
            request.org = membership.organization
            return view_func(request, *args, **kwargs)

        return login_required(_wrapped)

    return decorator
