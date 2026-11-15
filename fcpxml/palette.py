"""One palette for every surface fcp-mcp-server draws.

True black ground, one cyan accent, red only for failure and removal. The
MCP Apps timeline, the legacy ``preview://`` render, the waveform plate and
trace, the README hero, the social card and the README badges all read
their colours from :data:`TOKENS`; nothing else may type a colour.

``tests/test_palette.py`` holds the line. It fails when a hex colour appears
in a surface file without being a token here, when a token is neither a
neutral grey nor the accent nor the danger red (so amber, pink, purple,
cream and default blue cannot come back as a "new token"), when a text pair
drops under 4.5:1 contrast, and when a README badge is not on these values.

Each token is ``(dark, light)``. Light mode follows the host's theme
(``hostContext.theme``) or ``prefers-color-scheme``: a white ground, the same
cyan as a FILL under black ink, and a darker cyan (``#007A8A``) for accent
text and lines, because ``#00E5FF`` on white is unreadable.
"""

from __future__ import annotations

# name: (dark, light). Hex only: six digits, or eight where the last two are
# alpha. Neutrals are exact greys, r == g == b (0.32: they carried a blue tint,
# 18,18,22 and 28,28,34, read as off-black against the true-black ground); the
# accent and the danger red are the only colours with a hue.
TOKENS: dict[str, tuple[str, str]] = {
    "ground": ("#000000", "#FFFFFF"),       # page and app background
    "panel": ("#121212", "#F4F4F4"),        # cards, the track area, the waveform plate
    "raised": ("#1C1C1C", "#E4E4E4"),       # connected clips, buttons
    "raised-2": ("#2A2A2A", "#D9D9D9"),     # spine clips, titles
    "line": ("#262626", "#D4D4D4"),         # hairlines, lane rules
    "line-strong": ("#3A3A3A", "#A1A1A1"),  # clip borders, ticks, inputs
    "ink": ("#EDEDED", "#090909"),          # text
    "mute": ("#8C8C8C", "#5B5B5B"),         # secondary text
    "accent": ("#00E5FF", "#007A8A"),       # playhead, selection, markers, links, waveform
    "accent-fill": ("#00E5FF", "#00E5FF"),  # primary button fill (black ink on it)
    "accent-ink": ("#000000", "#000000"),   # text on accent-fill
    "accent-wash": ("#00E5FF1F", "#007A8A1F"),  # a selected range
    # The README badges only, and the ONE exception to "one cyan everywhere":
    # kept because the contrast rule needs it. shields.io chooses the badge
    # text itself, white on anything darker than brightness 0.69
    # ((299R + 587G + 114B) / 255000); #00E5FF is 0.64, so white text sat on it
    # at 1.8:1. This is the accent's own hue lifted just past that line, where
    # shields draws #333 (8.7:1). Every other surface uses accent / accent-fill.
    "badge": ("#33EBFF", "#33EBFF"),
    "on-media": ("#FFFFFF", "#FFFFFF"),     # a label drawn over a thumbnail
    "shade": ("#000000BF", "#000000BF"),    # the scrim under that label
    "danger": ("#FF3B30", "#D70015"),       # FAIL (and missing media) only: never an intended edit
    # Final Cut Pro 12.4's own role colours, the ONE sanctioned exception to
    # "grey, cyan, red" (decision 4, 2026-11-12): drawn as the 3 px role
    # stripe on a clip and nowhere else (never text, never a fill; held by
    # tests/test_palette.py). Read from the installed app, not typed from
    # memory: scripts/fcp_role_colours.m loads Flexo.framework and asks
    # FFColorForRole +colorSchemeForIndex: for each of FFRole
    # +builtInMainRoles (Video 0, Music 1, Effects 2, Titles 3, Dialogue 0).
    # Dark mode takes the scheme's roleFocusActiveColor (the role's colour in
    # Final Cut's timeline index, the brightest of the scheme, so it reads on
    # black); light mode its itemBackgroundColor (the clip body in Final Cut's
    # own timeline), which reads on white. Both converted to sRGB by AppKit.
    # Final Cut gives Video and Dialogue the same colour index, so they match.
    "role-video": ("#3763A1", "#1D3454"),
    "role-titles": ("#7E53D2", "#503585"),
    "role-dialogue": ("#3763A1", "#1D3454"),
    "role-music": ("#28A154", "#15542C"),
    "role-effects": ("#2A9FA9", "#17575C"),
}

ACCENT_HUE_TOKENS = ("accent", "accent-fill", "accent-wash", "badge")
DANGER_TOKENS = ("danger",)
ROLE_TOKENS = ("role-video", "role-titles", "role-dialogue", "role-music", "role-effects")

# Text-on-background pairs that must read at 4.5:1 or better, in both modes.
TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("ink", "ground"), ("ink", "panel"), ("ink", "raised"), ("ink", "raised-2"),
    ("mute", "ground"), ("mute", "panel"), ("mute", "raised"),  # secondary text; disabled buttons
    ("accent", "ground"), ("accent", "panel"),
    ("accent-ink", "accent-fill"),
    ("on-media", "shade"),
    ("danger", "ground"),
    ("ground", "mute"), ("ground", "ink"),  # names on the legacy preview's clip fills
)

# Fills a block sits on, against the ground it sits on: 3:1 or better, so a
# clip reads as a shape before its name is read (WCAG 1.4.11, non-text). The
# role stripes are held to it on the track area (the panel) in both modes.
FILL_PAIRS: tuple[tuple[str, str], ...] = (
    ("mute", "panel"), ("ink", "panel"),
    ("role-video", "panel"), ("role-titles", "panel"), ("role-dialogue", "panel"),
    ("role-music", "panel"), ("role-effects", "panel"),
)

# Where each role colour came from: (Final Cut role, FFColorForRole colour
# index, dark-mode scheme colour, light-mode scheme colour). The test holds
# TOKENS to it, and scripts/fcp_role_colours.m prints the same table from
# the installed app (tests/test_palette.py runs it when Final Cut is there).
ROLE_SOURCE = {
    "role-video": ("Video", 0, "roleFocusActiveColor", "itemBackgroundColor"),
    "role-titles": ("Titles", 3, "roleFocusActiveColor", "itemBackgroundColor"),
    "role-dialogue": ("Dialogue", 0, "roleFocusActiveColor", "itemBackgroundColor"),
    "role-music": ("Music", 1, "roleFocusActiveColor", "itemBackgroundColor"),
    "role-effects": ("Effects", 2, "roleFocusActiveColor", "itemBackgroundColor"),
}

# The README badges: black label, cyan message, dark text on the cyan.
BADGE_LABEL = "ground"
BADGE_MESSAGE = "badge"
SHIELDS_DARK_TEXT_ABOVE = 0.69  # shields.io's brightness threshold for #333 text


def shields_brightness(hex_value: str) -> float:
    """The brightness shields.io compares with SHIELDS_DARK_TEXT_ABOVE."""
    r, g, b = rgb(hex_value)
    return (r * 299 + g * 587 + b * 114) / 255000

# Hexes that left the surfaces in 0.31. Named so the test can say which one
# came back rather than only that something did.
RETIRED = (
    "#f59e0b", "#ec4899", "#eab308", "#8b5cf6", "#10b981", "#3b82f6", "#2563eb",
    "#06b6d4", "#f43f5e", "#2c2a22", "#0b0b0f", "#7b2cff", "#ff2c6b",
)


def dark(name: str) -> str:
    return TOKENS[name][0]


def light(name: str) -> str:
    return TOKENS[name][1]


def ffmpeg_colour(hex_value: str) -> str:
    """``#00E5FF`` -> ``0x00E5FF``, the spelling ffmpeg's colour options take."""
    return "0x" + hex_value.lstrip("#").upper()


def shields(name: str) -> str:
    """A token as shields.io takes it: six hex digits, no ``#``."""
    return dark(name).lstrip("#").upper()


def css_vars(mode: str = "dark") -> str:
    """``--ground: #000000; --panel: ...`` for one mode."""
    index = 0 if mode == "dark" else 1
    return " ".join(f"--{name}: {pair[index]};" for name, pair in TOKENS.items())


def css_root() -> str:
    """Both modes as CSS: dark by default, light when the host or the OS asks.

    ``data-theme`` is set by the app from ``hostContext.theme`` and beats the
    OS preference in either direction.
    """
    return (
        f":root {{ color-scheme: dark; {css_vars('dark')} }}\n"
        "  @media (prefers-color-scheme: light) {\n"
        f"    :root:not([data-theme=\"dark\"]) {{ color-scheme: light; {css_vars('light')} }}\n"
        "  }\n"
        f"  :root[data-theme=\"light\"] {{ color-scheme: light; {css_vars('light')} }}\n"
        f"  :root[data-theme=\"dark\"] {{ color-scheme: dark; {css_vars('dark')} }}"
    )


def as_json() -> dict[str, dict[str, str]]:
    """The tokens for a non-Python reader (the hero recorder, the card)."""
    return {"dark": {k: v[0] for k, v in TOKENS.items()}, "light": {k: v[1] for k, v in TOKENS.items()}}


# ---------------------------------------------------------------------------
# Arithmetic the test uses: WCAG 2 relative luminance and contrast.
# ---------------------------------------------------------------------------

def rgb(hex_value: str) -> tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def alpha(hex_value: str) -> float:
    h = hex_value.lstrip("#")
    return int(h[6:8], 16) / 255 if len(h) == 8 else 1.0


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_value: str) -> float:
    r, g, b = (_linear(c) for c in rgb(hex_value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def composite(top: str, under: str) -> str:
    """``top`` (which may carry alpha, ``#RRGGBBAA``) laid over the opaque ``under``."""
    a = alpha(top)
    mixed = (round(t * a + u * (1 - a)) for t, u in zip(rgb(top), rgb(under)))
    return "#" + "".join(f"{c:02X}" for c in mixed)


def is_neutral(hex_value: str, tolerance: int = 12) -> bool:
    r, g, b = rgb(hex_value)
    return max(r, g, b) - min(r, g, b) <= tolerance
