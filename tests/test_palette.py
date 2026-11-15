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
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from fcpxml import palette

REPO = Path(__file__).resolve().parent.parent

# Files that paint something a person sees. A new surface goes on this list.
SURFACES = [
    "fcpxml/timeline_app.py",
    "fcpxml/proofs.py",
    "fcpxml/raster.py",
    "demo/hero/build.py",
    "demo/hero/record.js",
    "scripts/social_card.py",
    "scripts/logo.py",
]

# `&#183;` is an HTML entity, not a colour; a hex must not follow & or a word char.
HEX = re.compile(r"(?<![&\w])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
FFMPEG_HEX = re.compile(r"\b0x[0-9a-fA-F]{6}\b")
# a CSS rgb()/rgba() literal; ``palette.rgb(token)`` (a Python call that turns a
# palette token into a tuple, used by the raster proofs) is not a colour literal
RGB_FN = re.compile(r"(?<![.\w])rgba?\(")

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
    from parts at run time). Render the app and the preview:// page (the app
    with a payload inlined, 0.32) and scan the HTML they actually serve."""
    from fcpxml import timeline_app
    from fcpxml.parser import FCPXMLParser

    assert off_palette(timeline_app.render_app_html()) == []
    project = FCPXMLParser().parse_file(str(REPO / "examples" / "music-video.fcpxml"))
    page = timeline_app.render_page_html(timeline_app.build_payload(project.timelines[0]))
    shell = page[:page.index('<script type="application/json"')] + page[page.index("</script>") + len("</script>"):]
    assert off_palette(shell) == []


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
            elif name in palette.ROLE_TOKENS:
                continue  # held to stripes below
            else:
                assert palette.is_neutral(value), (name, value)


ROLE_USE = re.compile(r"var\(--role-[a-z]+\)")


TITLE_BODY = re.compile(r"\.clip\.k-title \{[^}]*\}")


OVERVIEW_BAR = re.compile(r"#overview \.ob\.role-([a-z]+) \{ background: var\(--role-([a-z]+)\); \}")


def role_uses_off_stripe(text: str) -> list:
    """Lines that paint with a role colour anywhere but a clip's role stripe,
    the Titles colour on a title clip's body (0.34 round 3, as Final Cut
    draws a title), or (round 4) the overview minimap's bar for a clip of
    that same role. Any other role on a title body, any text, is caught."""
    out = []
    for line in text.splitlines():
        if not ROLE_USE.search(line) or ".stripe.role-" in line:
            continue
        bar = OVERVIEW_BAR.search(line)
        if bar and bar.group(1) == bar.group(2) and len(ROLE_USE.findall(line)) == 1:
            continue
        if TITLE_BODY.search(line) and set(ROLE_USE.findall(line)) == {"var(--role-titles)"} and "color: var(--role" not in line.replace("border-color", ""):
            continue
        out.append(line.strip())
    return out


def test_role_colours_paint_stripes_and_title_bodies_only():
    text = (REPO / "fcpxml" / "timeline_app.py").read_text(encoding="utf-8")
    assert ROLE_USE.search(text)
    assert role_uses_off_stripe(text) == []
    assert role_uses_off_stripe("  .clip .label { color: var(--role-music); }")
    assert role_uses_off_stripe("  .clip.k-title { background: var(--role-music); }")
    assert role_uses_off_stripe("  .clip.k-title { color: var(--role-titles); }")
    assert role_uses_off_stripe("  #overview .ob.role-video { background: var(--role-music); }")


FAILURE_ONLY = ("fcpxml/proofs.py", "fcpxml/raster.py", "tools/preview.py", "tools/scenes.py")


def red_uses(text: str) -> list:
    """Lines that paint with the danger red without saying which failure it
    marks (``# red: failure (...)`` on the call). The intended edit (a
    removed silence, a cut) is not a failure and is never red."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if "DANGER" not in line or line.lstrip().startswith(("#", "LINE,")) or "rs.colour(" in line:
            continue
        window = " ".join(lines[i:i + 2])
        if "# red: failure" not in window:
            out.append(f"{i + 1}: {line.strip()}")
    return out


@pytest.mark.parametrize("surface", FAILURE_ONLY)
def test_red_marks_only_a_failure(surface):
    """0.32 round 3: the check marked a REMOVED silence (the edit working)
    in red on its ruler. Every danger-red paint call names its failure."""
    assert red_uses((REPO / surface).read_text(encoding="utf-8")) == []


def test_the_red_rule_can_go_red():
    assert red_uses("            r.fill(x, y, w, 5, DANGER)\n")
    assert red_uses("            r.fill(x, y, w, 5, DANGER)  # red: failure (offline media)\n") == []


def test_neutral_tokens_are_exact_greys():
    """0.32: the panels were blue-tinted near-black (#121216, #1C1C22) and
    read as off-black beside the true-black ground. A neutral is r == g == b."""
    for name, pair in palette.TOKENS.items():
        if name in palette.ACCENT_HUE_TOKENS or name in palette.DANGER_TOKENS or name in palette.ROLE_TOKENS:
            continue
        for value in pair:
            r, g, b = palette.rgb(value)[:3]
            assert r == g == b, (name, value)


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


def test_the_fill_rule_can_go_red():
    """Mutation check: 0.31's first legacy-preview fill (raised on panel) was 1.15:1."""
    assert palette.contrast(palette.dark("raised"), palette.dark("panel")) < 3.0


def halo_rule(html: str) -> list[str]:
    """0.34 round 4 took the scrim pill from a name over a picture: its halo
    is a 1 px outline of the halo token on all eight sides plus soft shadows
    (tests/test_app_labels.py measures it over a white frame). What is
    missing from that rule."""
    rule = re.search(r"\.clip\.has-thumb \.label \{([^}]*)\}", html)
    if not rule:
        return ["no rule for a name over a picture"]
    shadow = re.search(r"text-shadow:([^;]*);", rule.group(1))
    if not shadow:
        return ["no halo"]
    parts = [p.split() for p in shadow.group(1).replace("\n", " ").split(",")]
    offsets = {(p[0], p[1]) for p in parts if len(p) == 4 and p[2] == "0" and p[3] == "var(--halo)"}
    need = {(x, y) for x in ("1px", "-1px", "0") for y in ("1px", "-1px", "0")} - {("0", "0")}
    out = [f"no outline at {x} {y}" for x, y in sorted(need - offsets)]
    if palette.dark("halo") != "#000000" or palette.light("halo") != "#000000":
        out.append("the halo is not black in both themes")
    return out


def test_a_name_over_a_picture_has_its_halo():
    from fcpxml import timeline_app

    assert halo_rule(timeline_app.render_app_html()) == []
    assert palette.contrast(palette.dark("on-media"), palette.dark("halo")) >= 4.5


def test_the_halo_rule_can_go_red():
    assert halo_rule(".clip.has-thumb .label { color: var(--on-media); }") == ["no halo"]
    assert halo_rule(".clip.has-thumb .label { color: var(--on-media); text-shadow: 0 0 3px var(--halo); }")


def test_text_over_a_thumbnail_is_the_label_colour():
    """Everything written inside a name over a picture (the name, the "off"
    tag of a disabled clip) is the label colour on the scrim: mute grey there
    read about 3:1. Since 0.34 the role is the stripe, never text."""
    from fcpxml import timeline_app

    html = timeline_app.render_app_html()
    label = re.search(r"\.clip\.has-thumb \.label \{([^}]*)\}", html)
    tag = re.search(r"\.clip\.has-thumb \.off-tag \{([^}]*)\}", html)
    assert label and "color: var(--on-media)" in label.group(1)
    assert tag and "color: var(--on-media)" in tag.group(1)
    assert ".role {" not in html


# ---------------------------------------------------------------------------
# Final Cut's role colours (0.34): read from the app, held to the stripe
# ---------------------------------------------------------------------------

ROLE_PROBE = REPO / "scripts" / "fcp_role_colours.m"
FCP_APP = Path("/Applications/Final Cut Pro.app")


def test_every_role_token_says_where_it_came_from():
    assert set(palette.ROLE_SOURCE) == set(palette.ROLE_TOKENS)
    for name, (role, index, column) in palette.ROLE_SOURCE.items():
        assert name == "role-" + role.lower() and isinstance(index, int)
        assert column == "roleFocusActiveColor"


def role_theme_problems(tokens: dict) -> list[str]:
    """0.34 round 4 (the Fable ruling): ONE set of role colours, Final Cut's
    own, in both themes. 035392a's light theme took the scheme's darker
    itemBackgroundColor (#1D3454 for #3763A1), which read as another colour."""
    return [f"{n}: dark {tokens[n][0]}, light {tokens[n][1]}" for n in palette.ROLE_TOKENS if tokens[n][0] != tokens[n][1]]


def test_the_role_colours_are_one_set_in_both_themes():
    assert role_theme_problems(palette.TOKENS) == []
    assert {palette.dark(n) for n in palette.ROLE_TOKENS} == {"#3763A1", "#7E53D2", "#28A154", "#2A9FA9"}


def test_the_one_set_check_goes_red():
    """Mutation check: 035392a's light role colours."""
    old = dict(palette.TOKENS, **{"role-video": ("#3763A1", "#1D3454"), "role-titles": ("#7E53D2", "#503585"),
                                  "role-music": ("#28A154", "#15542C"), "role-effects": ("#2A9FA9", "#17575C")})
    assert len(role_theme_problems(old)) == 4


def role_mismatches(table: str) -> list[str]:
    """Tokens that differ from the probe's table (role, index, colours...)."""
    lines = table.strip().splitlines()
    head = lines[0].split("\t")
    rows = {r.split("\t")[0]: dict(zip(head, r.split("\t"))) for r in lines[1:]}
    out = []
    for name, (role, index, column) in palette.ROLE_SOURCE.items():
        row = rows.get(role)
        if row is None:
            out.append(f"{role}: not a built-in role in this Final Cut")
            continue
        if int(row["index"]) != index:
            out.append(f"{role}: colour index {row['index']}, the palette says {index}")
        for mode, value in (("dark", palette.dark(name)), ("light", palette.light(name))):
            if row[column].upper() != value.upper():
                out.append(f"{name} {mode}: Final Cut says {row[column]}, the palette says {value}")
    return out


@pytest.mark.skipif(not FCP_APP.is_dir() or shutil.which("clang") is None,
                    reason="Final Cut Pro or clang not installed: the role colours are checked where they are")
def test_role_colours_are_final_cuts_own(tmp_path):
    """Compile scripts/fcp_role_colours.m, run it against the installed app
    (Flexo.framework loaded into a tool; no window, no Final Cut process) and
    hold every role token to what Final Cut computes."""
    tool = tmp_path / "fcp_role_colours"
    subprocess.run(["clang", "-fobjc-arc", "-framework", "Foundation", "-framework", "AppKit", "-o", str(tool),
                    str(ROLE_PROBE)], check=True, capture_output=True, timeout=120)
    env = dict(os.environ, DYLD_FRAMEWORK_PATH=str(FCP_APP / "Contents" / "Frameworks"))
    run = subprocess.run([str(tool), str(FCP_APP)], capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode == 0, run.stderr[-500:]
    assert role_mismatches(run.stdout) == [], run.stdout


def test_the_role_check_goes_red():
    """Mutation check: the 0.33 set-by-eye video blue, and a role whose index
    moved, are both seen against a table in the probe's shape."""
    table = ("role\tindex\tbaseColor\titemBackgroundColor\troleFocusActiveColor\taudioWaveformColor\n"
             + "\n".join(f"{r}\t{i}\t-\t-\t{palette.dark(n)}\t-"
                         for n, (r, i, _c) in palette.ROLE_SOURCE.items()))
    assert role_mismatches(table) == []
    assert role_mismatches(table.replace(palette.dark("role-video"), "#4A7FE0", 1))
    assert role_mismatches(table.replace("Music\t1", "Music\t4"))


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


def live_badge_problems(urls: list[str]) -> list[str]:
    """The tests badge is the live workflow status from shields (round 6), in
    the one tint: never a static "passing" that stays green when CI is red."""
    live = [u for u in urls if "/github/actions/workflow/status/" in u and "test.yml" in u]
    if not live:
        return ["no live tests badge (shields /github/actions/workflow/status/.../test.yml)"]
    return [u for u in urls if "label=tests" in u and u not in live]


def test_the_tests_badge_is_live():
    assert live_badge_problems(readme_badges((REPO / "README.md").read_text(encoding="utf-8"))) == []


def test_the_tests_badge_rule_can_go_red():
    assert live_badge_problems(["https://img.shields.io/static/v1?label=tests&message=passing&color=33EBFF"])


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
