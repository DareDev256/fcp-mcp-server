"""transcript_pack through the server: sidecar reuse, cap, write, truncation."""

import json
from pathlib import Path

import pytest

import server
from fcpxml import transcript_pack as _tpack
from tests.test_media_intel import PROJECT_XML


@pytest.fixture
def project(tmp_path):
    media = tmp_path / "interview.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    path = tmp_path / "project.fcpxml"
    path.write_text(PROJECT_XML.format(src=f"file://{media}"))
    return str(path), str(media)


def _sidecar(media, words, events=None):
    Path(media).with_name("interview_transcript.json").write_text(json.dumps({
        "language": "en", "duration": 5.0,
        "text": " ".join(w["word"] for w in words),
        "segments": [], "words": words, "events": events or [],
    }))


async def _call(args):
    out = await server.call_tool("transcript", {"action": "transcript_pack", "args": args})
    return out[0].text


async def test_pack_reads_the_sidecar_and_never_transcribes(project, monkeypatch):
    filepath, media = project
    _sidecar(media, [
        {"word": "we", "start": 0.0, "end": 0.2, "speaker": "S0"},
        {"word": "begin", "start": 0.2, "end": 0.5, "speaker": "S0"},
        {"word": "agreed", "start": 1.5, "end": 1.9, "speaker": "S1"},
    ], events=[{"start": 0.6, "end": 1.2, "label": "laughter"}])
    monkeypatch.setattr(server, "transcribe", lambda *a, **k: pytest.fail("transcribe called"))
    text = await _call({"filepath": filepath})
    assert "# interview.wav" in text
    assert "[0.00-0.50] S0 we begin" in text
    assert "[0.60-1.20] (laughter)" in text
    assert "[1.50-1.90] S1 agreed" in text
    assert "**Sources**: 1" in text
    assert "**Utterances**: 3" in text


async def test_missing_media_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "p.fcpxml"
    path.write_text(PROJECT_XML.format(src=f"file://{tmp_path}/gone.wav"))
    text = await _call({"filepath": str(path)})
    assert "**Sources**: 0" in text
    assert "media file missing" in text


async def test_write_saves_full_pack_beside_the_fcpxml(project):
    filepath, media = project
    _sidecar(media, [{"word": "hi", "start": 0.0, "end": 0.3}])
    text = await _call({"filepath": filepath, "write": True})
    out = Path(filepath).with_name("project_pack.md")
    assert out.is_file()
    assert str(out) in text
    assert out.read_text() == _tpack.pack([{"name": "interview.wav",
                                           "words": [{"word": "hi", "start": 0.0, "end": 0.3}],
                                           "events": []}])


async def test_oversized_pack_is_truncated_in_chat_but_written_whole(project, monkeypatch):
    filepath, media = project
    words = [{"word": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.4} for i in range(600)]
    _sidecar(media, words)
    monkeypatch.setattr(_tpack, "PACK_LIMIT_BYTES", 2000)
    text = await _call({"filepath": filepath, "write": True})
    assert "shown truncated to 2000" in text
    assert "… truncated (pack is" in text
    assert "w599" not in text.split("… truncated")[0]
    written = Path(filepath).with_name("project_pack.md").read_text()
    assert "w599" in written and "truncated" not in written


async def test_bad_gap_is_a_validation_error(project):
    filepath, _ = project
    text = await _call({"filepath": filepath, "gap": 9})
    assert text.startswith("Validation error")


async def test_gap_changes_line_breaks(project):
    filepath, media = project
    _sidecar(media, [{"word": "a", "start": 0.0, "end": 0.1},
                     {"word": "b", "start": 0.4, "end": 0.5}])
    joined = await _call({"filepath": filepath})
    split = await _call({"filepath": filepath, "gap": 0.2})
    assert "**Utterances**: 1" in joined
    assert "**Utterances**: 2" in split
