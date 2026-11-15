"""0.34: what the view tool sends, in bytes, through the real handler on either SDK.

Until 0.33 ``view_timeline`` fenced the whole payload into its text, base64
thumbnails and all: 11,184 characters for a five-clip cut, which a host
shows (and a model reads) as the tool's answer. Now the payload is the
result's ``structuredContent``, which the ext-apps spec hands to the app and
keeps out of the model's context, and the text is a short summary with the
clips listed. Held here:

- the text a client without Apps sees is under ``FALLBACK_TEXT_BUDGET``
  (2,000) characters for the five-clip fixture, with no picture in it, on
  the wire of the installed SDK (1.x and 2.x run the same test);
- the structured payload is under ``PAYLOAD_BUDGET`` on the 18-minute
  interview (44 cuts, every thumbnail and waveform);
- waveform peaks are measured once per file and cached in the index, and the
  same waveform comes back with ``FCP_MCP_INDEX=off``.

Each budget is broken on purpose once. No browser needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import server as server_module
import tools.view as view_mod
from fcpxml import timeline_app
from fcpxml.mcp_compat import is_legacy_api

REPO = Path(__file__).resolve().parent.parent
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg/ffprobe not installed")
FIVE = ("Opening", "Interview_Take2", "Studio", "B-roll street", "Closing")


def _wire(name: str, arguments: dict) -> dict:
    """Drive the registered tools/call handler, as a client would reach it,
    and return the result as it goes on the wire (by alias)."""
    import mcp.types as types

    if is_legacy_api():
        handler = server_module.server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(method="tools/call",
                                        params=types.CallToolRequestParams(name=name, arguments=arguments))
        result = asyncio.run(handler(request)).root
    else:
        entry = server_module.server._request_handlers["tools/call"]
        handler = getattr(entry, "handler", entry)
        result = asyncio.run(handler(None, types.CallToolRequestParams(name=name, arguments=arguments)))
    return json.loads(result.model_dump_json(by_alias=True, exclude_none=True))


@pytest.fixture(scope="module")
def five(tmp_path_factory):
    """Five clips on the spine from one synthetic file (testsrc2 and a sine):
    real thumbnails, real waveform peaks."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    out = tmp_path_factory.mktemp("five")
    media = out / "take.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=24:duration=20", "-f", "lavfi", "-i",
                    "sine=frequency=220:sample_rate=48000:duration=20", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)], check=True, timeout=120)
    edl = out / "five.json"
    edl.write_text(json.dumps({"sources": {n: str(media) for n in FIVE},
                               "ranges": [{"source": n, "start": 3 * i, "end": 3 * i + 2.5} for i, n in enumerate(FIVE)]}))
    path = out / "five.fcpxml"
    old = {k: os.environ.get(k) for k in ("FCP_MCP_JOURNAL", "FCP_MCP_INDEX")}
    os.environ.update(FCP_MCP_JOURNAL="off", FCP_MCP_INDEX="off")
    try:
        asyncio.run(server_module.call_tool("generate", {"action": "import_edl_json", "args": {
            "filepath": str(edl), "output_path": str(path), "name": "Five clips"}}))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert path.is_file()
    return path


@pytest.fixture(scope="module")
def thumbs_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("thumbs")  # one cache for the module: each picture rendered once


@pytest.fixture
def index_off(monkeypatch, thumbs_dir):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    monkeypatch.setattr(view_mod.render, "cache_dir", lambda: thumbs_dir)


def fallback_problems(result: dict) -> list[str]:
    texts = [c.get("text", "") for c in result["content"] if c.get("type") == "text"]
    out = []
    if sum(len(t) for t in texts) >= timeline_app.FALLBACK_TEXT_BUDGET:
        out.append(f"the text is {sum(len(t) for t in texts)} characters")
    if any("data:image" in t for t in texts):
        out.append("a picture in the text")
    return out


def test_the_text_for_five_clips_is_under_two_thousand_characters(five, index_off):
    result = _wire("view", {"action": "view_timeline", "args": {"filepath": str(five)}})
    assert fallback_problems(result) == []
    payload = result["structuredContent"]
    assert payload["schema"] == timeline_app.PAYLOAD_SCHEMA and len(payload["clips"]) == 5
    assert all(c["thumb"].startswith("data:image/jpeg;base64,") for c in payload["clips"])
    assert all(c.get("wave") for c in payload["clips"])
    assert timeline_app.payload_bytes(payload) <= timeline_app.PAYLOAD_BUDGET
    text = result["content"][0]["text"]
    for name in FIVE:  # a client without Apps still reads every clip
        assert name in text


def test_the_text_budget_goes_red_on_the_033_reply(five, index_off):
    """Mutation check: the 0.33 reply (the payload fenced into the text) for
    the same five clips is over the budget, and the instrument says so."""
    result = _wire("view", {"action": "view_timeline", "args": {"filepath": str(five)}})
    old = timeline_app.wrap_payload(result["content"][0]["text"], result["structuredContent"])
    assert fallback_problems({"content": [{"type": "text", "text": old}]})


def test_the_fallback_counts_what_it_does_not_list():
    p = {"fps": "24/1", "nominal_fps": 24, "clips": [
        {"start": f"{i}/1", "duration": "1/1", "lane": 0, "kind": "spine", "name": f"Clip {i}"} for i in range(20)]}
    text = timeline_app.fallback_text("summary", p)
    assert "12. 00:00:11:00 1.00s spine Clip 11" in text and "Clip 12" not in text
    assert text.endswith("...and 8 more (view_clip reads one by name).")


# ---- the payload, on the long fixture ---------------------------------------------

@pytest.fixture(scope="module")
def interview(tmp_path_factory):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    out = tmp_path_factory.mktemp("interview")
    env = dict(os.environ, FCP_MCP_JOURNAL="off", FCP_MCP_INDEX="off", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, str(REPO / "demo" / "fixtures" / "interview.py"), str(out)],
                   check=True, capture_output=True, env=env, timeout=900)
    return out / "interview.fcpxml"


def test_the_payload_of_the_long_interview_is_under_its_budget(interview, index_off):
    reply = asyncio.run(server_module.call_tool("view", {"action": "view_timeline", "args": {"filepath": str(interview)}}))
    payload = reply.structured
    assert len(payload["clips"]) == 58
    # every clip with a picture has it: the 44 cuts; the titles, the bed and the whooshes have none
    assert {c["kind"] for c in payload["clips"] if not c["thumb"]} == {"title", "audio"}
    assert sum(1 for c in payload["clips"] if c["thumb"]) == 44
    assert timeline_app.payload_bytes(payload) <= timeline_app.PAYLOAD_BUDGET, timeline_app.payload_bytes(payload)
    assert len(reply[0].text) < 3 * timeline_app.FALLBACK_TEXT_BUDGET  # twelve clips listed, the rest counted


def test_the_payload_budget_goes_red_with_a_full_rate_waveform(interview, index_off, monkeypatch):
    """Mutation check: peaks sent at 100 a second with no cap per clip (the
    short array made long) put the interview over its budget."""
    monkeypatch.setattr(timeline_app, "MAX_WAVE_POINTS", 10 ** 6)
    monkeypatch.setattr(timeline_app, "WAVE_POINTS_PER_SECOND", 100)
    reply = asyncio.run(server_module.call_tool("view", {"action": "view_timeline", "args": {"filepath": str(interview)}}))
    assert timeline_app.payload_bytes(reply.structured) > timeline_app.PAYLOAD_BUDGET


# ---- waveform peaks: measured once, cached, the same with the index off --------------

def _waves(path) -> list:
    reply = asyncio.run(server_module.call_tool("view", {"action": "view_timeline", "args": {
        "filepath": str(path), "thumbnails": False}}))
    return [c.get("wave") for c in reply.structured["clips"]]


def test_peaks_are_cached_in_the_index_and_the_same_without_it(five, monkeypatch, tmp_path):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    measured = []
    real = view_mod._measure_peaks
    monkeypatch.setattr(view_mod, "_measure_peaks", lambda p: measured.append(p) or real(p))
    monkeypatch.setenv("FCP_MCP_INDEX", str(tmp_path / "index.db"))
    first = _waves(five)
    after_first = len(measured)
    second = _waves(five)
    # the five clips share one file: measured once, then read from the index
    assert after_first == 1 and len(measured) == after_first, "measured more than once: no cache"
    assert first == second and all(w and len(w) == 25 == round(2.5 * timeline_app.WAVE_POINTS_PER_SECOND) for w in first)  # 2.5 s at 10 a second
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    assert _waves(five) == first
    assert len(measured) == after_first + 5  # off: measured for every clip, every time


def test_the_cache_check_goes_red(five, monkeypatch, tmp_path):
    """Mutation check: a reader that skips the index measures on every call."""
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    monkeypatch.setenv("FCP_MCP_INDEX", str(tmp_path / "index.db"))
    measured = []
    real = view_mod._measure_peaks
    monkeypatch.setattr(view_mod, "_measure_peaks", lambda p: measured.append(p) or real(p))
    monkeypatch.setattr(view_mod, "_file_peaks", view_mod._measure_peaks)
    _waves(five)
    _waves(five)
    assert len(measured) == 10


@needs_ffmpeg
def test_a_file_with_no_sound_has_no_waveform(tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    silent = tmp_path / "picture.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=24:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(silent)], check=True, timeout=60)
    assert view_mod._measure_peaks(str(silent)) == []
    assert view_mod._waveform(silent.as_uri(), 0, 1) is None
