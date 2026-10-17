"""The ``{% icon %}`` tag — inline SVG icons from Font Awesome Free.

Registered as a template builtin (see ``TEMPLATES['OPTIONS']['builtins']`` in
``config/settings/base.py``) so templates don't each need a ``{% load %}``: icons
appear in well over a hundred templates and base.html loads only ``static``.

Why a tag instead of ``<i class="fa-solid fa-trash">``: the webfont costs 117 KB
of woff2 plus 70 KB of CSS to ship ~2,000 icons when this app uses under a
hundred, and it flashes before the font arrives. Inlining keeps the zero-request
behaviour the templates already had, and ``fill="currentColor"`` means an icon
inherits whatever token-driven colour its container sets.
"""

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from apps.common.icons import BRAND_COLORS, ICONS, PLATFORM_ICONS

register = template.Library()


@register.simple_tag
def icon(name, size="5", css_class="", label=""):
    """Render ``name`` as an inline SVG.

    ``size`` is a Tailwind scale step, so ``size="4"`` yields ``w-4 h-4``.
    ``css_class`` replaces the sizing classes outright when you need something
    the scale doesn't express (``w-full``, a responsive size, ``flex-shrink-0``).

    ``label`` is for icons that carry meaning on their own — an icon-only button,
    a status glyph with no adjacent text. It exposes the icon to assistive
    technology as an image with that name. Without it the icon is marked
    ``aria-hidden``, which is what you want beside a visible text label: a screen
    reader announcing "trash Delete" is noise.

    Usage::

        {% icon "calendar-days" %}
        {% icon "trash" size="4" %}
        {% icon "spinner" css_class="w-4 h-4 animate-spin flex-shrink-0" %}
        {% icon "xmark" label="Close" %}
    """
    entry = ICONS.get(name)
    if entry is None:
        # Loud in the markup, harmless on the page — a missing icon should be
        # obvious while building and never take a page down in production.
        return format_html("<!-- unknown icon: {} -->", name)

    viewbox, path = entry
    a11y = format_html('role="img" aria-label="{}"', label) if label else mark_safe('aria-hidden="true"')
    # size="" means "don't size me" — for the icons a page's own stylesheet
    # already sizes through a descendant selector. Emitting w-5 h-5 there would
    # silently resize them, so the tag stays out of the way instead.
    classes = css_class or (f"w-{size} h-{size}" if size else "")
    class_attr = f'class="{escape(classes)}" ' if classes else ""
    return mark_safe(f'<svg {class_attr}viewBox="{viewbox}" fill="currentColor" {a11y}><path d="{path}"/></svg>')


@register.simple_tag
def platform_icon(platform, size="5", css_class="", label="", brand_color=False):
    """Render the brand mark for a platform key such as ``youtube``.

    Replaces two hand-maintained partials that had drifted into different icon
    sets, different size APIs and different platform coverage, so the same
    channel rendered differently depending on which one a template happened to
    include. One of them carried Google Business as a 6.3 KB base64 JPEG.

    Draws in ``currentColor`` by default: these marks sit in sidebars, disabled
    states and dark chips, and a fixed brand colour goes illegible in at least
    one of those — worse, a full-colour logo on a disabled channel reads as
    active.

    Pass ``brand_color=True`` where the logo is doing identification work and
    the background is light: a channel list, a platform picker, an account card.
    There, YouTube red and Instagram pink let someone find the right channel
    without reading a single label.

    Unknown platforms render nothing, which is the right outcome for a channel
    whose platform this build doesn't know about.

    Usage::

        {% platform_icon account.platform size="5" %}
        {% platform_icon account.platform size="4" brand_color=True %}
    """
    name = PLATFORM_ICONS.get(platform)
    if name is None:
        return format_html("<!-- no icon for platform: {} -->", platform)
    mark = icon(name, size=size, css_class=css_class, label=label)
    if not brand_color:
        return mark
    colour = BRAND_COLORS.get(platform)
    if colour is None:
        return mark
    # Set on the svg itself rather than a wrapper: a wrapper would change the
    # layout of every call site that already positions the icon.
    return mark_safe(mark.replace("<svg ", f'<svg style="color: {colour}" ', 1))
