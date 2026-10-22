"""Filtros de la bandeja: iniciales, tono de avatar y datos del tipo de publicación."""

import re

from django import template
from django.utils import timezone

from apps.inbox.panel import KIND_ICONS, VERTICAL_KINDS

register = template.Library()


@register.filter
def initials(name):
    """«Taller Rubio» -> «TR»; «@ithos.mx» -> «IT»."""
    name = (name or "?").strip()
    if name.startswith("@"):
        return name[1:3].upper() or "?"
    words = [w for w in re.split(r"[\s.@]+", name) if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


@register.filter
def tone(name):
    """Un tono de avatar estable por nombre (0 a 3)."""
    return len(name or "") % 4


@register.filter
def kind_icon(kind):
    return KIND_ICONS.get(kind, "comment")


@register.filter
def thumb_shape(kind):
    """Clase de forma de la miniatura: vertical para Short/Reel/Story, ancha para Video, cuadrada para el resto."""
    if kind in VERTICAL_KINDS:
        return "ib-th-v"
    return "" if kind == "Video" else "ib-th-s"


@register.filter
def ago(when):
    """«hace 3 h, 24 min» / «hace 3 h» / «hace 12 min» / «hace 2 d»: tiempo relativo corto y con espacios."""
    if not when:
        return ""
    mins = max(0, int((timezone.now() - when).total_seconds() // 60))
    if mins < 1:
        return "hace un momento"
    if mins < 60:
        return f"hace {mins} min"
    hours, rest = divmod(mins, 60)
    if hours < 24:
        return f"hace {hours} h, {rest} min" if rest else f"hace {hours} h"
    return f"hace {hours // 24} d"
