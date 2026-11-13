"""One palette, held by a test (0.31 "Front door").

Every surface a person sees takes its colours from ``fcpxml/palette.py``.
Until 0.31 there were four palettes on one product: purple (logo, a badge),
cyan (the app), amber (the social card) and pink-and-amber (the legacy HTML
timeline), plus six README badges in six colours. These tests make a fifth
impossible to add quietly:

- a hex colour (``#rrggbb``, ``#rrggbbaa``, ``#rgb``) or an ffmpeg colour
  (``0xrrggbb``) in a surface file must be a token value;
- ``rgb()``/``rgba()`` literals are refused outright (spell it as a token);
- a token must be a neutral grey, or the cyan accent, or the danger red;
- every text pair reads at 4.5:1 or better in both modes;
- the README's badges are at most three, on the token values, and none
  carries a version or a version range unless PyPI supplies it.

Each scanner is also run against a planted violation (``#f59e0b`` back in a
surface), so the check is seen going red, not only green.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from fcpxml import palette

REPO = Path(__file__).resolve().parent.parent

# Files that paint something a person sees. A new surface goes on this list.
SURFACES = [
    "fcpxml/timeline_app.py",
    "fcpxml/preview.py",
    "fcpxml/visual.py",
    "demo/hero/build.py",
    "demo/hero/record.js",
    "scripts/social_card.py",
    "scripts/logo.py",
]

# `&#183;` is an HTML entity, not a colour; a hex must not follow & or a word char.
HEX = re.compile(r"(?<![&\w])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
FFMPEG_HEX = re.compile(r"\b0x[0-9a-fA-F]{6}\b")
RGB_FN = re.compile(r"\brgba?\(")

TOKEN_VALUES = {v.upper() for pair in palette.TOKENS.values() for v in pair}


def off_palette(text: str) -> list[str]:
    """Every colour literal in *text* that is not a token value."""
    found = []
    for m in HEX.finditer(text):
        value = m.group(0).upper()
        if len(value) == 4:  # #rgb -> #rrggbb
            value = "#" + "".join(ch * 2 for ch in value[1:])
        if value not in TOKEN_VALUES:
            found.append(m.group(0))
    for m in FFMPEG_HEX.finditer(text):
        if "#" + m.group(0)[2:].upper() not in TOKEN_VALUES:
            found.append(m.group(0))
    found += [m.group(0) for m in RGB_FN.finditer(text)]
    return found


@pytest.mark.parametrize("surface", SURFACES)
def test_every_colour_in_a_surface_is_a_token(surface):
    text = (REPO / surface).read_text(encoding="utf-8")
    assert off_palette(text) == [], f"{surface} paints colours outside fcpxml/palette.py"


@pytest.mark.parametrize("surface", SURFACES)
def test_no_retired_colour_came_back(surface):
    text = (REPO / surface).read_text(encoding="utf-8").lower()
    back = [h for h in palette.RETIRED if h.lower() in text]
    assert back == [], f"{surface} brings back {back}"


def test_the_scanner_sees_a_retired_colour():
    """Mutation check: amber planted in a surface is caught, as a hex, as an
    ffmpeg colour and as an rgba(); an HTML entity is not mistaken for one."""
    planted = 'background: #f59e0b; color=c=0xF59E0B; box-shadow: rgba(0,229,255,.5); &#183;'
    assert off_palette(planted) == ["#f59e0b", "0xF59E0B", "rgba("]
    assert off_palette("&#183; &#215; #FFFFFF var(--accent)") == []


def test_the_rendered_surfaces_carry_only_tokens():
    """The files can be clean while what they RENDER is not (a colour built
    from parts at run time). Render the app and the legacy preview and scan
    the HTML they actually serve."""
    from fcpxml import timeline_app
    from fcpxml.parser import FCPXMLParser
    from fcpxml.preview import render_timeline_html

    assert off_palette(timeline_app.render_app_html()) == []
    project = FCPXMLParser().parse_file(str(REPO / "examples" / "music-video.fcpxml"))
    assert off_palette(render_timeline_html(project.timelines[0])) == []


def _hue_degrees(hex_value: str) -> float:
    r, g, b = (c / 255 for c in palette.rgb(hex_value))
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360


def test_tokens_are_grey_or_cyan_or_red():
    """A new token cannot smuggle a hue in: only the accent tokens may be
    cyan and only danger may be red; everything else is a neutral grey."""
    for name, pair in palette.TOKENS.items():
        for value in pair:
            if name in palette.ACCENT_HUE_TOKENS:
                assert 180 <= _hue_degrees(value) <= 195, (name, value)
            elif name in palette.DANGER_TOKENS:
                hue = _hue_degrees(value)
                assert hue <= 10 or hue >= 350, (name, value)
            else:
                assert palette.is_neutral(value), (name, value)


def test_the_hue_rule_can_go_red():
    """Mutation check: amber and purple are not neutral and not cyan."""
    for value in ("#F59E0B", "#8B5CF6", "#3B82F6"):
        assert not palette.is_neutral(value)
        assert not 180 <= _hue_degrees(value) <= 195


def test_retired_colours_are_not_tokens():
    assert not ({h.upper() for h in palette.RETIRED} & TOKEN_VALUES)


@pytest.mark.parametrize("mode", [0, 1], ids=["dark", "light"])
@pytest.mark.parametrize("fg,bg", palette.TEXT_PAIRS)
def test_text_reads_at_4_5_to_1(fg, bg, mode):
    ratio = palette.contrast(palette.TOKENS[fg][mode], palette.TOKENS[bg][mode])
    assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f}:1"


@pytest.mark.parametrize("mode", [0, 1], ids=["dark", "light"])
@pytest.mark.parametrize("fill,ground", palette.FILL_PAIRS)
def test_block_fills_read_at_3_to_1(fill, ground, mode):
    ratio = palette.contrast(palette.TOKENS[fill][mode], palette.TOKENS[ground][mode])
    assert ratio >= 3.0, f"{fill} on {ground}: {ratio:.2f}:1"


def legacy_fills(html: str) -> list[str]:
    """The fill tokens the legacy preview paints its clip blocks with."""
    rules = re.findall(r"\.clip[^{]*\{([^}]*)\}", html)
    return [m for rule in rules for m in re.findall(r"background:\s*var\(--([\w-]+)\)", rule)]


def test_the_legacy_preview_paints_blocks_with_3_to_1_fills():
    from fcpxml.parser import FCPXMLParser
    from fcpxml.preview import render_timeline_html

    html = render_timeline_html(FCPXMLParser().parse_file(str(REPO / "examples" / "music-video.fcpxml")).timelines[0])
    fills = legacy_fills(html)
    assert fills and all((f, "panel") in palette.FILL_PAIRS for f in fills), fills
    assert "inset 0 3px 0 var(--accent)" in html  # the thin accent strip on top


def test_the_fill_rule_can_go_red():
    """Mutation check: 0.31's first legacy fill (raised on panel) was 1.15:1."""
    assert palette.contrast(palette.dark("raised"), palette.dark("panel")) < 3.0


@pytest.mark.parametrize("frame", ["#FFFFFF", "#FFFF00", "#00FFFF", "#000000"])
def test_a_clip_label_reads_over_any_frame(frame):
    """A label over a thumbnail sits on the shade scrim, whatever the frame
    is: worst case a white frame, where 0.31's half-faded scrim left 'Studio'
    at 2.7:1."""
    under = palette.composite(palette.dark("shade"), frame)
    assert palette.contrast(palette.dark("on-media"), under) >= 4.5


def scrim_rule(html: str) -> tuple[float, float]:
    """(where the scrim reaches full shade, where the label text starts), in px."""
    rule = re.search(r"\.clip\.has-thumb \.label \{([^}]*)\}", html).group(1)
    full = float(re.search(r"var\(--shade\)\s+([\d.]+)px", rule).group(1))
    text = float(re.search(r"padding-top:\s*([\d.]+)px", rule).group(1))
    return full, text


def test_the_scrim_is_full_before_the_text_starts():
    from fcpxml import timeline_app

    full, text = scrim_rule(timeline_app.render_app_html())
    assert full <= text, f"the scrim is still fading where the text starts ({full}px > {text}px)"


def test_text_over_a_thumbnail_is_the_label_colour():
    """The role text sits on the same scrim: mute grey there read about 3:1."""
    from fcpxml import timeline_app

    html = timeline_app.render_app_html()
    rule = re.search(r"\.clip\.has-thumb \.role \{([^}]*)\}", html)
    assert rule and "color: var(--on-media)" in rule.group(1)


def disabled_button_problems(html: str) -> list[str]:
    rule = re.search(r"button:disabled \{([^}]*)\}", html)
    if not rule:
        return ["no button:disabled rule"]
    problems = []
    if "opacity" in rule.group(1):
        problems.append("button:disabled fades with opacity (a 45% fade read at 4.1:1)")
    if "color: var(--mute)" not in rule.group(1):
        problems.append("button:disabled text is not the mute token")
    return problems


def test_disabled_buttons_read_at_4_5_to_1():
    """The mute token is held at 4.5:1 on the raised button fill by TEXT_PAIRS;
    this holds the CSS to that token instead of an opacity fade."""
    from fcpxml import timeline_app

    assert ("mute", "raised") in palette.TEXT_PAIRS
    assert disabled_button_problems(timeline_app.render_app_html()) == []


def test_the_disabled_rule_can_go_red():
    assert disabled_button_problems("button:disabled { opacity: .45; cursor: default; }")


def test_the_scrim_rules_can_go_red():
    """Mutation checks: a 25% scrim fails the arithmetic, and a fade that
    ends below the text top fails the layout rule."""
    assert palette.contrast(palette.dark("on-media"), palette.composite("#00000040", "#FFFFFF")) < 4.5
    full, text = scrim_rule(".clip.has-thumb .label { padding-top: 12px; background: linear-gradient(transparent, var(--shade) 30px); }")
    assert full > text


def test_the_contrast_rule_can_go_red():
    """Mutation check: the old light-mode idea of cyan text on white fails."""
    assert palette.contrast("#00E5FF", "#FFFFFF") < 4.5
    assert palette.contrast("#007A8A", "#FFFFFF") >= 4.5


# ---------------------------------------------------------------------------
# README badges
# ---------------------------------------------------------------------------

BADGE = re.compile(r"https://img\.shields\.io/[^)\s\"']+")


def readme_badges(text: str) -> list[str]:
    return BADGE.findall(text)


def badge_problems(urls: list[str]) -> list[str]:
    problems = []
    if len(urls) > 3:
        problems.append(f"{len(urls)} badges; the front door holds three")
    want = {"color": palette.shields(palette.BADGE_MESSAGE),
            "labelColor": palette.shields(palette.BADGE_LABEL)}
    for url in urls:
        query = parse_qs(urlparse(url).query)
        for key, value in want.items():
            got = (query.get(key) or [""])[0].upper()
            if got != value:
                problems.append(f"{url}: {key}={got or '(default)'}, want {value}")
    return problems


def test_readme_badges_are_three_on_the_palette():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert badge_problems(readme_badges(text)) == []
    # and nothing else draws a badge in its own colour
    assert "actions/workflows/test.yml/badge.svg" not in text


def test_badge_text_is_dark_on_the_cyan():
    """shields.io decides the text colour from the background: the badge
    colour must be past its brightness line, or the text is white at 1.8:1."""
    value = palette.dark(palette.BADGE_MESSAGE)
    assert palette.shields_brightness(value) > palette.SHIELDS_DARK_TEXT_ABOVE
    assert palette.contrast("#333333", value) >= 4.5


def test_the_badge_text_rule_can_go_red():
    """Mutation check: the accent itself is under the line (white text)."""
    assert palette.shields_brightness(palette.dark("accent")) <= palette.SHIELDS_DARK_TEXT_ABOVE


VERSION = re.compile(r"\d+\.\d+")


def stale_version_badges(urls: list[str]) -> list[str]:
    """A badge that prints a version, or a range of them ("10.4-12.4"), must
    ask PyPI for it, never carry it: an image cannot be grepped or tested."""
    return [u for u in urls if "/pypi/v/" not in u and VERSION.search(unquote(urlparse(u).path + urlparse(u).query))]


def test_the_version_badge_is_dynamic():
    urls = readme_badges((REPO / "README.md").read_text(encoding="utf-8"))
    assert any("/pypi/v/fcp-mcp-server" in u for u in urls)
    assert stale_version_badges(urls) == []


def test_the_dynamic_rule_can_go_red():
    assert stale_version_badges(["https://img.shields.io/static/v1?label=PyPI&message=v0.30.0&color=33EBFF"])
    # the round-4 Final Cut badge: a range baked into an image
    assert stale_version_badges(["https://img.shields.io/static/v1?label=Final%20Cut%20Pro"
                                 "&message=Mac%2010.4%E2%80%9312.4&color=33EBFF&labelColor=000000"])


def test_the_badge_rule_can_go_red():
    """Mutation check: the 0.30 badge row fails it on count and on colour."""
    old = [
        "https://img.shields.io/pypi/v/fcp-mcp-server.svg",
        "https://img.shields.io/badge/python-3.10+-blue.svg",
        "https://img.shields.io/badge/MCP%20SDK-1.21.1%20%7C%202.x-green.svg",
        "https://img.shields.io/badge/Final%20Cut%20Pro-10.4%E2%80%9312.x-purple.svg",
    ]
    problems = badge_problems(old)
    assert any("4 badges" in p for p in problems)
    assert any("color=(default)" in p for p in problems)
