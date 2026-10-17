"""Every workspace-scoped view that changes something must check a permission.

`RBACMiddleware` raises `PermissionDenied` for a non-member, so there is no
tenant leak here — the gap this guards is narrower and easier to miss: a view
that any *member* can call regardless of role. A `viewer` uploading media, a
`contributor` deleting a publishing queue.

Ten such views were found the first time this ran. Adding an eleventh is a
one-line mistake with no visible symptom, which is exactly what a coverage test
is for.

Views that do the check inside their own body (some need the object first to
know which permission applies) count as covered.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]

DECORATOR_GATES = {
    "require_permission",
    "require_workspace_role",
    "require_org_role",
    "require_org_permission",
}
# Markers that a view resolves the permission itself rather than by decorator.
# Some views need the object in hand before they know which permission applies
# — workspace settings gates renaming on ownership, not on a static key.
INLINE_GATES = (
    "effective_permissions",
    "require_permission",
    "PermissionDenied",
    "workspace_role",
    "is_owner",
    # Shared helpers that raise on their own. Named explicitly so moving a check
    # into a function does not read as removing it.
    "may_edit_post",
    "_require_may_edit",
)
# Deliberately not a bare ".update(" — dict.update() on a template context is
# not a database write, and matching it made calendar_view a false positive.
#
# The service-layer names are here because their absence hid a real hole:
# `remove_pending_media` destroyed assets through `delete_asset()`, which
# contains no ".delete(" of its own, so this test classified the view as
# read-only and skipped it entirely. Any write that goes through a helper is
# invisible here unless the helper is named.
MUTATES = (
    ".save(",
    ".delete(",
    ".create(",
    ".bulk_create(",
    ".get_or_create(",
    ".objects.update(",
    ".update_or_create(",
    "delete_asset(",
    "clone_post(",
)

# Views that legitimately change state without a role check, with the reason.
ALLOWED = {
    # Acts on the caller's own record only; role is irrelevant to dismissing
    # your own onboarding checklist.
    "onboarding.dismiss_checklist",
}


def _decorator_names(node):
    names = []
    for d in node.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        names.append(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", ""))
    return names


def _workspace_views():
    """(app, name, decorators, source) for every workspace-scoped view."""
    found = []
    # `views*.py` and not `views.py`: apps/client_portal/views_admin.py and
    # apps/accounts/views_signup.py were never scanned at all.
    for path in sorted(REPO.glob("apps/*/views*.py")) + sorted(REPO.glob("apps/*/views/*.py")):
        app = path.relative_to(REPO).parts[1]
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.parse(source).body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            args = [a.arg for a in node.args.args]
            if not args or args[0] != "request" or "workspace_id" not in args:
                continue
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            found.append((app, node.name, _decorator_names(node), body))
    return found


WORKSPACE_VIEWS = _workspace_views()


def test_the_audit_actually_finds_views():
    """A refactor that moves views elsewhere must not silently empty this test."""
    assert len(WORKSPACE_VIEWS) > 50, f"only found {len(WORKSPACE_VIEWS)} workspace views"


@pytest.mark.parametrize(
    "app,name,decorators,body",
    [pytest.param(*v, id=f"{v[0]}.{v[1]}") for v in WORKSPACE_VIEWS],
)
def test_mutating_views_check_a_permission(app, name, decorators, body):
    if f"{app}.{name}" in ALLOWED:
        return
    if not any(marker in body for marker in MUTATES):
        return  # read-only: membership from the middleware is enough
    gated = set(decorators) & DECORATOR_GATES or any(g in body for g in INLINE_GATES)
    assert gated, (
        f"{app}.{name} writes to the database but checks no role or permission, "
        f"so any member of the workspace can call it. Add a @require_permission "
        f"decorator, or add it to ALLOWED here with the reason."
    )
