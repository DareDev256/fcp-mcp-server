"""The README's timeline picture refuses to be empty (0.34 round 3, F14).

Both critics scored the README picture 2: it was the music video drawn from
its FCPXML alone, whose media does not exist here, so every clip was an
empty block, the bed had no waveform, and none of its markers showed.
``scripts/readme_timeline.py`` now draws the GYM cut (real media that belongs
to it) and saves ``docs/assets/timeline-preview.png`` only when every
picture clip has a filmstrip that is not flat, every clip with sound has its
waveform, and the markers drawn are as many as the FCPXML holds. Here its
check runs on the old empty render and goes red, and on the gym cut and
passes. The browser is required in CI's rendered-labels job.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fcpxml import timeline_app
from fcpxml.parser import FCPXMLParser

REQUIRED = os.environ.get("FCP_MCP_REQUIRE_BROWSER") == "1"
if REQUIRED:
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")
pytest.importorskip("PIL")

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("readme_timeline", REPO / "scripts" / "readme_timeline.py")
readme_timeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readme_timeline)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser not installed here
            if REQUIRED:
                raise
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def gym(tmp_path_factory):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    out = tmp_path_factory.mktemp("gym")
    env = dict(os.environ, FCP_MCP_JOURNAL="off", FCP_MCP_INDEX="off", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, str(REPO / "demo" / "fixtures" / "gym_session.py"), str(out)],
                   check=True, capture_output=True, env=env, timeout=600)
    return out / "edited.fcpxml"


def _problems(browser, payload: dict, fcpxml: str) -> list[str]:
    page = browser.new_page(viewport={"width": readme_timeline.WIDTH, "height": 400}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payload))
        page.wait_for_timeout(100)
        return readme_timeline.problems(page, readme_timeline.expected_markers(fcpxml))
    finally:
        page.close()


def test_the_old_empty_render_is_refused(browser):
    """The 0.33 README picture: the music video with no media. Refused: no
    filmstrip on any picture clip and no waveform on the bed."""
    fcpxml = str(REPO / "examples" / "music-video.fcpxml")
    tl = FCPXMLParser().parse_file(fcpxml).timelines[0]
    found = _problems(browser, timeline_app.build_payload(tl), fcpxml)
    assert any("no filmstrip" in f for f in found) and any("no waveform" in f for f in found), found


def test_a_flat_filmstrip_and_a_missing_marker_are_refused(browser, gym, monkeypatch):
    """The two other refusals: a picture that is one flat colour (a white
    frame where the thumbnail should be), and a marker the page did not draw."""
    import base64
    import io

    from PIL import Image

    from tools import view

    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (255, 255, 255)).save(buf, "PNG")
    flat = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    tl = FCPXMLParser().parse_file(str(gym)).timelines[0]
    payload = timeline_app.build_payload(tl, thumbnail=lambda m, at: flat, waveform=view._waveform)
    payload["clips"][0]["markers"] = []  # the page draws one marker fewer than the file holds
    found = _problems(browser, payload, str(gym))
    assert any("flat" in f for f in found) and any("markers drawn" in f for f in found), found


def test_the_gym_cut_is_saved(browser, gym, monkeypatch, tmp_path):
    from tools import view

    monkeypatch.setattr(view.render, "cache_dir", lambda: tmp_path)
    tl = FCPXMLParser().parse_file(str(gym)).timelines[0]
    payload = timeline_app.build_payload(tl, thumbnail=view._thumbnail, waveform=view._waveform)
    assert _problems(browser, payload, str(gym)) == []
