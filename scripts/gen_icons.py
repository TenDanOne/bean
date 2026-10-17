"""Extract the icons this app uses from the installed Font Awesome Free package.

Writes apps/common/icons.py. Regenerate by adding names to WANTED and re-running;
node_modules is not a runtime dependency, the generated module is.
"""

import pathlib
import re

FA = pathlib.Path("theme/static_src/node_modules/@fortawesome/fontawesome-free/svgs")
OUT = pathlib.Path("apps/common/icons.py")

WANTED = {
    "solid": [
        # Navigation
        "house",
        "lightbulb",
        "pen-to-square",
        "calendar-days",
        "inbox",
        "chart-line",
        "bell",
        "plug",
        "gear",
        "users",
        "folder-open",
        "folder",
        "clipboard",
        "list",
        "bolt",
        "circle-minus",
        "music",
        "book",
        "circle-question",
        "face-frown",
        "desktop",
        "arrow-down-wide-short",
        "arrow-up-wide-short",
        "rotate-right",
        "code-branch",
        "table-columns",
        "cloud-arrow-up",
        "image",
        "layer-group",
        "table-cells",
        # Actions
        "plus",
        "xmark",
        "check",
        "pencil",
        "trash",
        "copy",
        "arrow-rotate-right",
        "paper-plane",
        "eye",
        "eye-slash",
        "download",
        "upload",
        "magnifying-glass",
        "filter",
        "ellipsis",
        "ellipsis-vertical",
        "arrow-right",
        "arrow-left",
        "chevron-down",
        "chevron-up",
        "chevron-left",
        "chevron-right",
        "link",
        "share-nodes",
        "rotate-left",
        "arrow-up-right-from-square",
        "grip-vertical",
        "arrows-rotate",
        "scissors",
        "crop-simple",
        "bars",
        "circle-plus",
        "arrow-up-from-bracket",
        "floppy-disk",
        "clone",
        "square-check",
        # Status
        "circle-check",
        "circle-xmark",
        "circle-exclamation",
        "triangle-exclamation",
        "circle-info",
        "clock",
        "spinner",
        "hourglass-half",
        "ban",
        "lock",
        "shield-halved",
        "circle-pause",
        "circle-play",
        "play",
        "pause",
        # Content
        "video",
        "photo-film",
        "file-lines",
        "comment",
        "comments",
        "message",
        "at",
        "hashtag",
        "quote-left",
        "thumbs-up",
        "heart",
        "star",
        "bookmark",
        "flag",
        "tag",
        "tags",
        "reply",
        "align-left",
        # People and account
        "user",
        "user-plus",
        "user-group",
        "right-from-bracket",
        "key",
        "envelope",
        "globe",
        "sliders",
        "list-check",
        "calendar-check",
        "paperclip",
        "face-smile",
        "robot",
        "wand-magic-sparkles",
        "building",
        "id-card",
        "arrow-right-arrow-left",
    ],
    # Brand marks, emitted with a brand- prefix. These replace two divergent
    # hand-maintained partials, one of which carried a 6.3 KB base64 JPEG.
    "brands": [
        "facebook",
        "instagram",
        "linkedin",
        "tiktok",
        "youtube",
        "pinterest",
        "threads",
        "bluesky",
        "mastodon",
        "dev",
        "google",
        "x-twitter",
    ],
}

# providers/__init__.py platform key -> brand mark. Several platforms share one
# mark on purpose: a personal and a company LinkedIn are the same logo.
PLATFORMS = {
    "facebook": "facebook",
    "instagram": "instagram",
    "instagram_login": "instagram",
    "linkedin": "linkedin",
    "linkedin_personal": "linkedin",
    "linkedin_company": "linkedin",
    "twitter": "x-twitter",
    "x": "x-twitter",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "pinterest": "pinterest",
    "threads": "threads",
    "bluesky": "bluesky",
    "mastodon": "mastodon",
    "devto": "dev",
    "google_business": "google",
}

# Official brand colours, applied only where the logo is doing identification
# work — a channel list, a picker, an account card — via {% platform_icon
# brand_color=True %}. Everywhere else the mark inherits currentColor, because a
# fixed hex goes illegible on a dark chip and makes a disabled channel look
# active. Sourced from each platform's own brand guidelines.
BRAND_COLORS = {
    "facebook": "#0866FF",
    "instagram": "#E4405F",
    "instagram_login": "#E4405F",
    "linkedin": "#0A66C2",
    "linkedin_personal": "#0A66C2",
    "linkedin_company": "#0A66C2",
    "twitter": "#000000",
    "x": "#000000",
    "tiktok": "#000000",
    "youtube": "#FF0000",
    "pinterest": "#BD081C",
    "threads": "#000000",
    "bluesky": "#0285FF",
    "mastodon": "#6364FF",
    "devto": "#0A0A0A",
    "google_business": "#4285F4",
}

# The icons this replaced were thin outlines and the interface is going for a
# soft, low-contrast feel, so every name under "solid" resolves to Font
# Awesome's *regular* (outline) cut when one exists and falls back to solid
# otherwise. FA Free draws only 273 icons in regular, but they are the common
# ones — which is exactly where the weight difference shows.
#
# Except these: a filled shape is the point. A spinner reads as motion only with
# some mass, and an outlined status dot looks empty rather than "on".
FORCE_SOLID = {
    "spinner",
    "circle-play",
    "circle-pause",
    "grip-vertical",
    "ellipsis",
    "ellipsis-vertical",
}

VIEWBOX = re.compile(r'viewBox="([^"]+)"')
PATH_D = re.compile(r'<path[^>]*\bd="([^"]+)"')


def source_for(style, name):
    """Resolve a wanted icon to a file, preferring the outline cut."""
    if style == "solid" and name not in FORCE_SOLID:
        outline = FA / "regular" / f"{name}.svg"
        if outline.exists():
            return outline
    return FA / style / f"{name}.svg"


rows, missing, outlined = [], [], 0
for style, names in WANTED.items():
    for name in names:
        f = source_for(style, name)
        if not f.exists():
            missing.append(f"{style}/{name}")
            continue
        if f.parent.name == "regular":
            outlined += 1
        s = f.read_text(encoding="utf-8")
        vb, d = VIEWBOX.search(s), PATH_D.search(s)
        if not (vb and d):
            missing.append(f"{style}/{name} (unparsed)")
            continue
        key = name if style == "solid" else f"brand-{name}"
        rows.append((key, vb.group(1), d.group(1)))

if missing:
    print("MISSING:", ", ".join(missing))

rows.sort()
body = "\n".join(f"    {k!r}: ({vb!r}, {d!r})," for k, vb, d in rows)
OUT.write_text(
    '"""Icon paths extracted from Font Awesome Free 7.3.1.\n\n'
    "Generated — do not hand-edit. To add an icon, append its name to WANTED in\n"
    "scripts/gen_icons.py and re-run it.\n\n"
    "Inlined as SVG rather than loaded as a webfont on purpose: the font costs\n"
    "117 KB of woff2 plus 70 KB of CSS to ship roughly 2,000 icons when this app\n"
    "uses a few dozen, and it introduces a flash of unstyled icons that the\n"
    "existing inline-SVG approach does not have.\n\n"
    "Each name resolves to Font Awesome's regular (outline) cut when one exists\n"
    "and to solid otherwise, so the set stays as light as FA Free allows. Brand\n"
    "marks are prefixed brand-.\n\n"
    "Icons: CC BY 4.0 (https://fontawesome.com/license/free) (c) Fonticons, Inc.\n"
    '"""\n\n'
    "ICONS: dict[str, tuple[str, str]] = {\n" + body + "\n}\n\n"
    "# Platform key (see providers/__init__.py) -> key in ICONS above. Several\n"
    "# platforms share one mark on purpose: a personal and a company LinkedIn\n"
    "# are the same logo, and Instagram's two connection methods are one brand.\n"
    "PLATFORM_ICONS: dict[str, str] = {\n"
    + "\n".join(f"    {k!r}: {'brand-' + v!r}," for k, v in sorted(PLATFORMS.items()))
    + "\n}\n\n"
    "# Official brand colours, used only where the logo identifies a channel.\n"
    "# See {% platform_icon %} for when this applies and why it isn't the default.\n"
    "BRAND_COLORS: dict[str, str] = {\n"
    + "\n".join(f"    {k!r}: {v!r}," for k, v in sorted(BRAND_COLORS.items()))
    + "\n}\n",
    encoding="utf-8",
)
print(f"{len(rows)} icons -> {OUT}  ({outlined} outline, {len(rows) - outlined} solid or brand)")
