"""Template helpers for the approval-workflow UI."""

from django import template

register = template.Library()

# Presentation metadata per editorial status: label + the ``st-*`` class that
# sets the colour tokens (--st-*) in styles.css. Kept here so every approval
# surface (publish tab, composer panel, client portal) renders an identical badge.
STATUS_META = {
    "draft": {"label": "Borrador", "css": "draft"},
    "pending_review": {"label": "En revisión", "css": "review"},
    "pending_client": {"label": "Pendiente del cliente", "css": "review"},
    "approved": {"label": "Aprobado", "css": "approved"},
    "changes_requested": {"label": "Cambios solicitados", "css": "review"},
    "rejected": {"label": "Rechazado", "css": "failed"},
    "scheduled": {"label": "Programado", "css": "scheduled"},
    "publishing": {"label": "Publicando", "css": "publishing"},
    "published": {"label": "Publicado", "css": "published"},
    "partially_published": {"label": "Parcialmente publicado", "css": "review"},
    "failed": {"label": "Falló", "css": "failed"},
    "on_hold": {"label": "En pausa", "css": "paused"},
}


@register.inclusion_tag("approvals/partials/_status_badge.html")
def status_badge(status):
    """Render the editorial-status pill (label) for *status*."""
    return {"meta": STATUS_META.get(status, STATUS_META["draft"])}
