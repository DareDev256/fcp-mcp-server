"""find_index / find_shots / find_to_timeline through the MCP seam.

No transcription, no network, no vision model in CI: every result must name
which tiers answered and which could not.
"""

import json
import os
from fractions import Fraction
from pathlib import Path

import pytest

import server
import tools
from fcpxml import diversity, vlm
from fcpxml import index as _index
from tests.conftest import requires_index

PROJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="10s" hasVideo="1" hasAudio="1">
      <media-rep kind="original-media" src="{a}"/>
    </asset>
    <asset id="r3" name="beach" start="0s" duration="30s" hasVideo="1" hasAudio="0">
      <media-rep kind="original-media" src="{b}"/>
    </asset>
  </resources>
  <library>
    <event name="Test">
      <project name="FindTest">
        <sequence format="r1" duration="40s" tcStart="0s">
          <spine>
            <asset-clip ref="r2" offset="0s" name="interview" start="0s" duration="10s" audioRole="dialogue">
              <marker start="7s" duration="1/24s" value="pickup here" note="best take"/>
            </asset-clip>
            <asset-clip ref="r3" offset="10s" name="Beach Wide" start="0s" duration="30s">
              <keyword start="0s" duration="30s" value="sunset, b-roll"/>
            </asset-clip>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

WORDS = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(
    "so the budget was the real problem and we fixed the budget by cutting scope".split())]


@pytest.fixture
def project(tmp_path, monkeypatch):
    a = tmp_path / "interview.mov"
    b = tmp_path / "beach.mov"
    a.write_bytes(b"\x00" * 64)
    b.write_bytes(b"\x00" * 64)
    Path(tmp_path / "interview_transcript.json").write_text(json.dumps({
        "language": "en", "duration": 10.0, "text": " ".join(w["word"] for w in WORDS),
        "segments": [], "words": WORDS, "events": [{"start": 8.0, "end": 9.0, "label": "laughter"}],
    }))
    path = tmp_path / "project.fcpxml"
    path.write_text(PROJECT_XML.format(a=f"file://{a}", b=f"file://{b}"))
    # Media is bytes, not video: no scene backend can read it, and nothing may transcribe.
    monkeypatch.setattr(server, "transcribe", lambda *a, **k: pytest.fail("transcribe called"))
    monkeypatch.setattr(vlm, "available", lambda: False)
    return str(path)


async def _call(action, **args):
    return await server.call_tool("find", {"action": action, "args": args})


def test_advertised_actions():
    assert set(tools.EXTRA_GROUPS["find"]["actions"]) == {"find_index", "find_shots", "find_to_timeline"}


async def test_find_shots_transcript_tier_names_mode_and_timecodes(project):
    text = (await _call("find_shots", filepath=project, query="budget problem"))[0].text
    first = text.splitlines()[0]
    assert first.startswith("Mode: transcript + metadata") and "vision unavailable" in first
    assert "fcp-mcp-server[find]" in first
    assert "| transcript |" in text and "said:" in text
    assert "| 1 | interview | 1.000s | 5.900s | 1.000s |" in text


async def test_find_shots_metadata_tier(project):
    text = (await _call("find_shots", filepath=project, query="sunset"))[0].text
    assert "| metadata |" in text and "keyword:" in text and "Beach Wide" in text
    text = (await _call("find_shots", filepath=project, query="pickup"))[0].text
    assert "marker: pickup here" in text and "| 7.000s |" in text
    text = (await _call("find_shots", filepath=project, query="laughter"))[0].text
    assert "event: laughter" in text and "| 8.000s | 9.000s |" in text


async def test_find_shots_no_hits_says_what_was_searched(project):
    text = (await _call("find_shots", filepath=project, query="zebra"))[0].text
    assert "No shots matched 'zebra'" in text and "transcript" in text and "find_index" in text


async def test_find_shots_reports_missing_transcripts(project):
    text = (await _call("find_shots", filepath=project, query="budget"))[0].text
    assert "**No transcript** (1): Beach Wide" in text and "transcript_media" in text


async def test_find_shots_refuses_to_imply_semantic_search(project):
    """Spec §10 'refuses': fallback mode must be named, never implied away."""
    text = (await _call("find_shots", filepath=project, query="wide shot of the beach"))[0].text
    assert "vision unavailable" in text.splitlines()[0]


async def test_find_shots_names_missing_model(project, monkeypatch, tmp_path):
    monkeypatch.setattr(vlm, "available", lambda: True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    text = (await _call("find_shots", filepath=project, query="wide shot"))[0].text
    assert f"hf download {vlm.DEFAULT_MODEL}" in text.splitlines()[0]


@requires_index
async def test_find_shots_vision_tier_uses_cached_captions(project, tmp_path):
    with _index.Index.open() as ix:
        ix.put_shots(str(tmp_path / "beach.mov"), [
            {"start": 0.0, "end": 5.0, "caption": "wide shot of a beach at sunset"},
            {"start": 5.0, "end": 9.0, "caption": "close up of a coffee cup"},
        ])
    text = (await _call("find_shots", filepath=project, query="wide shot of the beach"))[0].text
    assert text.splitlines()[0] == "Mode: transcript + metadata + vision"
    assert "| vision |" in text and "looks like: wide shot of a beach" in text
    assert "coffee" not in text


async def test_find_shots_validation(project):
    assert "Validation error" in (await _call("find_shots", filepath=project, query=""))[0].text
    assert "Validation error" in (await _call("find_shots", filepath=project, query="x", limit=0))[0].text


async def test_find_index_reports_tiers_and_missing_transcripts(project):
    text = (await _call("find_index", filepath=project, captions=False))[0].text
    assert "**Transcripts**: 1 of 2 clips" in text and "missing for Beach Wide" in text
    assert "**Scenes**:" in text and "**Captions**: skipped" in text
    text = (await _call("find_index", filepath=project))[0].text
    assert "**Captions**: unavailable" in text and "fcp-mcp-server[find]" in text


async def test_find_index_never_transcribes_or_downloads(project, monkeypatch, tmp_path):
    monkeypatch.setattr(vlm, "available", lambda: True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(vlm, "caption_shots", lambda *a, **k: pytest.fail("captioned without a model"))
    text = (await _call("find_index", filepath=project))[0].text
    assert "hf download" in text


async def test_find_to_timeline_assembles_with_diversity(project):
    text = (await _call("find_to_timeline", filepath=project, query="budget"))[0].text
    assert text.startswith("Mode:") and "Saved to" in text and "_found.fcpxml" in text and "Diversity:" in text
    out = project.replace(".fcpxml", "_found.fcpxml")
    assert os.path.exists(out)
    assert 'ref="r2"' in Path(out).read_text()


async def test_find_to_timeline_no_hits_writes_nothing(project):
    text = (await _call("find_to_timeline", filepath=project, query="zebra"))[0].text
    assert "Nothing written" in text
    assert not os.path.exists(project.replace(".fcpxml", "_found.fcpxml"))


async def test_find_to_timeline_diversity_mutation(project, monkeypatch):
    """Delete diversity.apply from the assembly path and this goes red."""
    called = {}

    def spy(shots, **k):
        called["yes"] = True
        return shots

    monkeypatch.setattr(diversity, "apply", spy)
    await _call("find_to_timeline", filepath=project, query="budget")
    assert called.get("yes")


async def test_find_shots_live_captions_are_stored_once(project, monkeypatch, tmp_path):
    """With a loadable model, uncaptioned sources are captioned live (bounded) and cached."""
    monkeypatch.setattr(vlm, "available", lambda: True)
    monkeypatch.setattr(vlm, "model_cached", lambda m=None: True)
    calls = []

    def fake_caption_shots(media_path, shots, *, model=None, max_frames=40, progress=None):
        calls.append((Path(media_path).name, max_frames))
        assert max_frames <= 20  # MAX_LIVE_FRAMES
        return [{"start": Fraction(s), "end": Fraction(e), "caption": "wide shot of the sky"} for s, e in shots]

    monkeypatch.setattr(vlm, "caption_shots", fake_caption_shots)
    text = (await _call("find_shots", filepath=project, query="wide shot"))[0].text
    assert text.splitlines()[0] == "Mode: transcript + metadata + vision"
    assert "looks like: wide shot of the sky" in text
    # Only the clip that survived tiers 1-2 ("Beach Wide" by name) was captioned.
    assert calls == [("beach.mov", 20)]
    if _index.enabled():
        await _call("find_shots", filepath=project, query="wide shot")
        assert len(calls) == 1  # second query hit the cache
