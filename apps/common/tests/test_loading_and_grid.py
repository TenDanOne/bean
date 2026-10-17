"""The connect-a-platform grid, and the loading bar that replaced the skeletons.

A grid that loses its `minmax(0, 1fr)` scrolls the whole page sideways the first
time someone connects a platform with a long name, and neither the CSS nor the
template makes that obvious on inspection.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
STYLES = REPO / "theme" / "static_src" / "src" / "styles.css"
TEMPLATES = REPO / "templates"
BASE = TEMPLATES / "base.html"
CONNECT = TEMPLATES / "social_accounts" / "connect.html"


def read(path):
    return path.read_text(encoding="utf-8")


class TestLoadingBar:
    def test_the_reveal_is_delayed(self):
        """A bar that flashes for 80ms reads as a glitch. The delay lives in the
        transition, so a response landing sooner paints nothing at all."""
        css = read(STYLES)
        rule = re.search(r'\[aria-busy="true"\]::before\s*\{([^}]*)\}', css)
        assert rule, "the loading bar is gone"
        delay = re.search(r"transition:\s*opacity[^;]*?(\d+)ms\s*;", rule.group(1))
        assert delay, "the loading bar has no delay in its transition"
        assert int(delay.group(1)) >= 120

    def test_only_big_surfaces_are_watched(self):
        """A bar over a button or a counter is noise."""
        js = read(BASE)
        assert "#tab-content" in js
        assert "WATCHED" in js

    def test_the_state_is_always_cleared(self):
        """An errored or timed-out request must not leave a surface stuck."""
        js = read(BASE)
        for event in ("htmx:afterSwap", "htmx:responseError", "htmx:sendError", "htmx:timeout"):
            assert event in js, f"{event} does not clear the loading state"

    def test_no_skeletons_remain(self):
        """The skeleton system was removed; these are the traces it would leave."""
        for path in TEMPLATES.rglob("*.html"):
            source = read(path)
            assert "data-skeleton" not in source, f"{path.name} still declares a skeleton"
            assert "skeleton-layer" not in source, f"{path.name} still has a skeleton layer"
        assert not (TEMPLATES / "partials" / "skeletons").exists()

    def test_reduced_motion_stops_the_sweep(self):
        css = read(STYLES)
        tail = css[css.index("LAYER 13") :]
        block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*)", tail, re.S)
        assert block, "layer 13 has no reduced-motion branch"
        assert "animation: none" in block.group(1)


class TestConnectGrid:
    def test_the_grid_reaches_four_columns(self):
        assert "xl:grid-cols-4" in read(CONNECT)

    def test_the_container_is_wide_enough_for_four(self):
        """max-w-4xl is 896px; four cards there would be 212px each, too narrow
        for the longest platform description."""
        source = read(CONNECT)
        assert "max-w-4xl" not in source
        assert "max-w-7xl" in source

    def test_grid_tracks_cannot_overflow(self):
        """Tailwind's grid-cols-N compiles to repeat(N, minmax(0, 1fr)). That
        minmax(0, ...) is what stops a long platform name widening a track past
        its share and scrolling the page sideways."""
        compiled = REPO / "theme" / "static" / "css" / "dist" / "styles.css"
        if not compiled.exists():
            pytest.skip("stylesheet not built")
        css = compiled.read_text(encoding="utf-8")
        assert "minmax(0,1fr)" in css.replace(" ", ""), "the compiled grid utilities lost minmax(0, 1fr)"

    def test_cards_can_shrink_below_their_content(self):
        """min-w-0 on the text column is the flexbox half of the same fix."""
        assert "min-w-0" in read(CONNECT)
