import json

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(is_safe=True)
def json_attr(value):
    """Serialize a Python value as a JSON literal safe to embed inside an
    HTML attribute (e.g. Alpine.js x-data).

    json.dumps produces JSON, then HTML-escape covers &, <, >, ", '. The
    browser HTML-decodes the attribute value before the JS engine parses
    it, so Alpine still sees valid JSON.

    Pass Python values (list/dict/None), NOT pre-serialized JSON strings.
    None and empty-string (Django's `string_if_invalid` fallback when a
    template variable like `post.tags` doesn't resolve) both become `[]`,
    matching the prior `|default:'[]'|safe` idiom.
    """
    if value is None or value == "":
        return mark_safe(escape("[]"))
    return mark_safe(escape(json.dumps(value, ensure_ascii=False, default=str)))


@register.filter
def role_es(value):
    """Etiqueta en español de un rol (owner, admin, manager...). Valores desconocidos pasan tal cual."""
    from apps.members.models import OrgMembership, WorkspaceMembership

    labels = {**dict(OrgMembership.OrgRole.choices), **dict(WorkspaceMembership.WorkspaceRole.choices)}
    return labels.get(value, value)


@register.filter
def can(request, permission_key):
    """True when the current workspace membership grants ``permission_key``.

    Lets templates disable an action rather than hide it, so a lower role still
    sees what exists and why it's out of reach. Reads the same
    ``effective_permissions`` dict the ``@require_permission`` decorator enforces,
    so the UI can't drift from the backend — this is presentation only, never the
    security boundary.

    Usage: ``{% if request|can:"manage_social_accounts" %}``
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None:
        return False
    return bool(membership.effective_permissions.get(permission_key, False))


@register.simple_tag(takes_context=True)
def can_on(context, social_account, capability):
    """True when the viewer holds ``capability`` on this specific channel.

    The sibling of ``can``: that one answers "may your role do this at all",
    this one answers "on this channel". Both have to pass, so a template showing
    an action needs both — `{% if request|can:"reply_from_inbox" %}` and
    `{% can_on message.social_account "can_reply" as may_reply %}`.

    A tag rather than a filter because it takes two arguments, and it reads the
    same ``can_on_channel`` the decorators enforce so the button and the endpoint
    cannot disagree. Presentation only; never the boundary.
    """
    from apps.members.channels import can_on_channel

    request = context.get("request")
    membership = getattr(request, "workspace_membership", None)
    if membership is None or social_account is None:
        return False
    return can_on_channel(membership, social_account, capability)


@register.filter
def json_list(values):
    """A Python list of strings as a JavaScript array literal.

    For Alpine expressions that need a list from the view. Building one by hand
    with a ``{% for %}`` and comma logic is where a stray trailing comma breaks
    the whole component with an error nobody sees except in the console.

    Escaped for embedding in an HTML attribute, so an id containing a quote
    cannot end the attribute early.
    """
    return mark_safe(escape(json.dumps([str(v) for v in (values or [])])))
