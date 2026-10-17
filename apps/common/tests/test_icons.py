"""Tests for the {% icon %} tag and the generated Font Awesome icon set."""

import re

import pytest
from django.template import Context, Template

from apps.common.icons import BRAND_COLORS, ICONS, PLATFORM_ICONS
from providers import PROVIDER_REGISTRY


def render(source, **context):
    return Template(source).render(Context(context))


class TestIconTag:
    def test_is_a_builtin_no_load_needed(self):
        """The tag has to work without {% load %}.

        It is used in templates that load no tag library at all; if the builtin
        registration in TEMPLATES['OPTIONS'] is ever dropped, every one of those
        raises TemplateSyntaxError at render time rather than at startup.
        """
        assert "<svg" in render('{% icon "calendar-days" %}')

    def test_renders_inline_svg_with_currentcolor(self):
        out = render('{% icon "trash" %}')
        assert out.startswith("<svg")
        assert 'fill="currentColor"' in out
        assert "<path d=" in out
        # No webfont, no external reference: the whole point of inlining.
        assert "<use" not in out and "fa-" not in out

    def test_size_maps_to_tailwind_scale(self):
        assert 'class="w-4 h-4"' in render('{% icon "trash" size="4" %}')
        assert 'class="w-5 h-5"' in render('{% icon "trash" %}')

    def test_css_class_replaces_sizing(self):
        out = render('{% icon "spinner" css_class="w-4 h-4 animate-spin" %}')
        assert 'class="w-4 h-4 animate-spin"' in out
        assert "w-5 h-5" not in out

    def test_empty_size_emits_no_class_at_all(self):
        """Some icons are sized by a page's own stylesheet through a descendant
        selector. Emitting w-5 h-5 for those would silently resize them."""
        out = render('{% icon "at" size="" %}')
        assert "<svg viewBox=" in out
        assert "class=" not in out
        assert "w-5" not in out

    def test_decorative_by_default(self):
        """Beside a visible text label, an icon must not be announced twice."""
        out = render('{% icon "trash" %}')
        assert 'aria-hidden="true"' in out
        assert "aria-label" not in out

    def test_label_exposes_icon_to_assistive_tech(self):
        out = render('{% icon "xmark" label="Close" %}')
        assert 'role="img"' in out
        assert 'aria-label="Close"' in out
        assert "aria-hidden" not in out

    def test_label_is_escaped(self):
        out = render('{% icon "xmark" label=evil %}', evil='"><script>x</script>')
        assert "<script>" not in out

    def test_unknown_icon_degrades_to_a_comment(self):
        """A typo should be visible while building and harmless in production."""
        out = render('{% icon "no-such-icon" %}')
        assert out.strip() == "<!-- unknown icon: no-such-icon -->"
        assert "<svg" not in out


class TestGeneratedIconSet:
    def test_every_entry_is_a_usable_viewbox_and_path(self):
        assert ICONS, "icon set is empty — did scripts/gen_icons.py run?"
        viewbox = re.compile(r"^0 0 \d+ \d+$")
        for name, (vb, path) in ICONS.items():
            assert viewbox.match(vb), f"{name} has a malformed viewBox: {vb!r}"
            assert path.strip(), f"{name} has an empty path"
            assert '"' not in path, f"{name} path would break the attribute quoting"

    @pytest.mark.parametrize(
        "name",
        ["calendar-days", "inbox", "chart-line", "bell", "gear", "trash", "check"],
    )
    def test_icons_the_navigation_depends_on_are_present(self, name):
        assert name in ICONS


class TestPlatformIcon:
    def test_every_registered_provider_has_a_brand_mark(self):
        """No connected channel should render without its logo.

        Adding a platform to PROVIDER_REGISTRY without adding its mark is easy
        to miss — the channel just renders blank — so this fails the build
        instead.
        """
        missing = sorted(set(PROVIDER_REGISTRY) - set(PLATFORM_ICONS))
        assert not missing, f"platforms with no brand mark: {missing}"

    def test_every_mapped_mark_actually_exists(self):
        for platform, name in PLATFORM_ICONS.items():
            assert name in ICONS, f"{platform} maps to missing icon {name!r}"

    def test_renders_the_mark_in_currentcolor(self):
        out = render('{% platform_icon "youtube" %}')
        assert "<svg" in out
        # currentColor, not YouTube red: these sit in sidebars and disabled chips.
        assert 'fill="currentColor"' in out
        assert "#FF0000" not in out.upper()

    def test_platforms_sharing_a_brand_render_identically(self):
        assert render('{% platform_icon "linkedin_personal" %}') == render('{% platform_icon "linkedin_company" %}')
        assert render('{% platform_icon "instagram" %}') == render('{% platform_icon "instagram_login" %}')

    def test_brand_color_is_opt_in(self):
        """Default must stay currentColor — a coloured logo on a disabled
        channel reads as active, and a fixed hex dies on a dark chip."""
        assert "style=" not in render('{% platform_icon "youtube" %}')

    def test_brand_color_applies_the_official_hex(self):
        out = render('{% platform_icon "youtube" brand_color=True %}')
        assert "color: #FF0000" in out
        assert 'fill="currentColor"' in out  # the hex arrives via color, not fill

    def test_brand_color_survives_a_custom_class(self):
        out = render('{% platform_icon "tiktok" css_class="w-8 h-8" brand_color=True %}')
        assert 'class="w-8 h-8"' in out
        assert "color: #000000" in out

    def test_every_platform_with_a_mark_has_a_colour(self):
        missing = sorted(set(PLATFORM_ICONS) - set(BRAND_COLORS))
        assert not missing, f"platforms with a mark but no brand colour: {missing}"

    def test_unknown_platform_renders_nothing_visible(self):
        out = render('{% platform_icon "myspace" %}')
        assert "<svg" not in out

    def test_both_legacy_partials_resolve_to_the_same_mark(self):
        """The two partials were separate icon sets; they must not diverge again."""
        via_scale = render(
            '{% include "social_accounts/partials/_platform_icon.html" with platform="tiktok" size="4" %}'
        )
        via_pixels = render('{% include "partials/_platform_icon.html" with platform="tiktok" size="sm" %}')
        assert via_scale.strip() == via_pixels.strip()

    def test_pixel_api_still_distinguishes_sm_from_md(self):
        sm = render('{% include "partials/_platform_icon.html" with platform="youtube" size="sm" %}')
        md = render('{% include "partials/_platform_icon.html" with platform="youtube" size="md" %}')
        assert "w-4 h-4" in sm
        assert "w-5 h-5" in md
