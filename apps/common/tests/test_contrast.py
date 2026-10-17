"""WCAG contrast floors for the design tokens.

The neumorphic pass deliberately lowered contrast everywhere — that is what
makes a surface look moulded rather than drawn. It is also exactly how an
interface becomes unreadable one well-meant tweak at a time, and nobody notices
until someone reads an inbox for an afternoon on a dim laptop.

So the floors live here rather than in a designer's memory. Three real defects
were found the first time this ran: white on the brand orange at 2.8:1, the
lightest text grey at 2.5:1 while carrying 257 colour classes, and a form-field
border at 1.2:1 with a soft shadow for company.
"""

import pathlib
import re

import pytest

STYLES = pathlib.Path(__file__).resolve().parents[3] / "theme" / "static_src" / "src" / "styles.css"

AA_NORMAL = 4.5  # WCAG 1.4.3 for body text
AA_LARGE = 3.0  # WCAG 1.4.3 for >=18.66px bold or >=24px
NON_TEXT = 3.0  # WCAG 1.4.11 for boundaries that identify a control


def _tokens():
    css = STYLES.read_text(encoding="utf-8")
    root = css[css.index(":root {") : css.index("/* Base */")]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{3,8}|var\(--[\w-]+\)|rgb\([^)]*\))", root))


TOKENS = _tokens()


def resolve(name, depth=0):
    """Follow var() indirection down to an (r, g, b) triple."""
    value = TOKENS.get(name)
    assert value is not None, f"token {name} is not defined in styles.css"
    assert depth < 10, f"token {name} resolves in a loop"
    ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if ref:
        return resolve(ref.group(1), depth + 1)
    hexa = re.fullmatch(r"#([0-9A-Fa-f]{6})", value)
    if hexa:
        return tuple(int(hexa.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    rgb = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value)
    assert rgb, f"token {name} has an unparseable value: {value!r}"
    return tuple(int(g) for g in rgb.groups())


def luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    hi, lo = sorted((luminance(resolve(fg)), luminance(resolve(bg))), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


SURFACES = ["--surface-0", "--surface-1", "--surface-2", "--surface-page"]


@pytest.mark.parametrize(
    "token",
    ["--text-primary", "--text-strong", "--text-body", "--text-secondary", "--text-tertiary", "--text-ghost"],
)
@pytest.mark.parametrize("surface", SURFACES)
def test_body_text_is_readable_on_every_surface(token, surface):
    """Anything that can hold words has to clear AA on any panel it lands on."""
    ratio = contrast(token, surface)
    assert ratio >= AA_NORMAL, f"{token} on {surface} is {ratio:.2f}:1, below AA's {AA_NORMAL}:1"


def test_text_faint_is_documented_as_decoration_only():
    """--text-faint is below AA by design; this pins that it stays out of the
    readable scale so nobody promotes it to body text by accident."""
    assert contrast("--text-faint", "--surface-0") < AA_LARGE


def test_text_on_the_brand_orange_clears_aa():
    """The primary action button. White here measured 2.8:1 — the brand orange
    is too light to carry white text, and the orange is the identity, so the
    text is what changed."""
    ratio = contrast("--on-primary", "--primary")
    assert ratio >= AA_NORMAL, f"text on the brand orange is {ratio:.2f}:1"


def test_form_field_borders_identify_the_control():
    """WCAG 1.4.11. A field edge under 3:1 plus a soft inner shadow is a form
    where you cannot tell where the input begins."""
    for surface in ["--surface-0", "--surface-1"]:
        ratio = contrast("--border-control", surface)
        assert ratio >= NON_TEXT, f"--border-control on {surface} is {ratio:.2f}:1"


@pytest.mark.parametrize(
    "fg,bg",
    [
        ("--error-700", "--error-50"),
        ("--success-700", "--success-50"),
        ("--warning-700", "--warning-50"),
        ("--info-700", "--info-50"),
    ],
)
def test_status_text_is_readable_on_its_own_tint(fg, bg):
    """A failed publish or a rate-limit warning is the worst possible place for
    text somebody has to squint at."""
    ratio = contrast(fg, bg)
    assert ratio >= AA_NORMAL, f"{fg} on {bg} is {ratio:.2f}:1"
