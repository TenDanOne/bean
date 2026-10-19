"""Filtros de la bandeja: iniciales, tono de avatar y datos del tipo de publicación."""

import re

from django import template

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
