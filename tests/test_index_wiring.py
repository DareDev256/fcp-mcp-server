"""The index sits beside the analysis functions, never inside them.

Each test here asks the same question twice: does the second call skip the
expensive work with the index on, and does it still get the right answer
with the index off? A cache that is load-bearing would fail the second half.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from fcpxml import mcp_compat
from tests.conftest import requires_index
from tests.test_media_intel import PROJECT_XML


@pytest.fixture
def project(tmp_path):
    media = tmp_path / "interview.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    path = tmp_path / "project.fcpxml"
    path.write_text(PROJECT_XML.format(src=f"file://{media}"))
    return str(path), str(media)


class TestSilenceThroughTheIndex:
    @requires_index
    async def test_second_call_does_not_touch_ffmpeg(self, project, monkeypatch):
        filepath, media = project
        calls = []

        def fake(path, noise_db, min_duration):
            calls.append(path)
            return [(1.0, 3.0)]

        monkeypatch.setattr(server, "detect_silence", fake)
        a = await server.handle_detect_media_silence({"filepath": filepath})
        b = await server.handle_detect_media_silence({"filepath": filepath})
        assert len(calls) == 1
        assert a[0].text == b[0].text
        assert "11.0" in a[0].text and "13.0" in a[0].text

    @requires_index
    async def test_different_threshold_is_a_different_answer(self, project, monkeypatch):
        filepath, media = project
        calls = []
        monkeypatch.setattr(server, "detect_silence", lambda p, noise_db, min_duration: calls.append(noise_db) or [])
        await server.handle_detect_media_silence({"filepath": filepath, "noise_db": -30.0})
        await server.handle_detect_media_silence({"filepath": filepath, "noise_db": -40.0})
        assert calls == [-30.0, -40.0]

    async def test_index_off_hits_ffmpeg_every_time(self, project, monkeypatch):
        """Mutation target: make the index load-bearing and this reads the same
        with it off — which is exactly what the FCP_MCP_INDEX=off CI job exists
        to catch across the whole suite."""
        filepath, media = project
        monkeypatch.setenv("FCP_MCP_INDEX", "off")
        calls = []
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: calls.append(p) or [(1.0, 3.0)])
        a = await server.handle_detect_media_silence({"filepath": filepath})
        b = await server.handle_detect_media_silence({"filepath": filepath})
        assert len(calls) == 2
        assert "11.0" in a[0].text and a[0].text == b[0].text

    async def test_a_rewritten_source_is_reprobed(self, project, monkeypatch):
        filepath, media = project
        calls = []
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: calls.append(p) or [])
        await server.handle_detect_media_silence({"filepath": filepath})
        st = os.stat(media)
        os.utime(media, (st.st_atime, st.st_mtime + 10))
        await server.handle_detect_media_silence({"filepath": filepath})
        assert len(calls) == 2

    async def test_a_failed_probe_is_not_cached(self, project, monkeypatch):
        filepath, media = project
        calls = []
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: calls.append(p) or None)
        await server.handle_detect_media_silence({"filepath": filepath})
        await server.handle_detect_media_silence({"filepath": filepath})
        assert len(calls) == 2


class TestBeatsThroughTheIndex:
    @requires_index
    async def test_second_call_skips_librosa(self, tmp_path, monkeypatch):
        wav = tmp_path / "track.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 64)
        calls = []
        monkeypatch.setattr(
            server, "detect_beats", lambda p: calls.append(p) or {"bpm": 120.0, "beats": [0.5, 1.0, 1.5]}
        )
        a = await server.handle_detect_beats({"media_path": str(wav)})
        b = await server.handle_detect_beats({"media_path": str(wav)})
        assert len(calls) == 1
        assert "120.0 BPM" in a[0].text and a[0].text == b[0].text


class TestTranscriptsThroughTheIndex:
    @requires_index
    async def test_sidecar_gone_index_answers(self, project, monkeypatch):
        filepath, media = project
        calls = []
        data = {
            "language": "en", "duration": 4.0, "text": "hello world",
            "segments": [{"text": "hello world", "start": 0.0, "end": 1.0}],
            "words": [{"word": "hello", "start": 0.0, "end": 0.5}, {"word": "world", "start": 0.5, "end": 1.0}],
        }
        monkeypatch.setattr(server, "transcribe", lambda p, model_size, language, backend="local": calls.append(p) or data)
        first, _ = server._load_or_transcribe(media, "base", None)
        sidecar = Path(media).with_name("interview_transcript.json")
        assert sidecar.is_file()
        sidecar.unlink()
        second, _ = server._load_or_transcribe(media, "base", None)
        assert len(calls) == 1
        assert [w["word"] for w in second["words"]] == ["hello", "world"]

    async def test_sidecar_still_wins_when_present(self, project, monkeypatch):
        filepath, media = project
        sidecar = Path(media).with_name("interview_transcript.json")
        sidecar.write_text(json.dumps({"text": "from sidecar", "words": [], "segments": []}))
        monkeypatch.setattr(server, "transcribe", lambda *a, **k: pytest.fail("should not transcribe"))
        data, _ = server._load_or_transcribe(media, "base", None)
        assert data["text"] == "from sidecar"


class TestProgressIsEmitted:
    async def test_silence_scan_reports_per_clip(self, project, monkeypatch):
        filepath, media = project
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: [])

        class Session:
            def __init__(self):
                self.calls = []

            async def send_progress_notification(self, token, progress, total=None, message=None, **_):
                self.calls.append((progress, total, message))

        session = Session()
        ctx = SimpleNamespace(session=session, meta=SimpleNamespace(progressToken="t"), request_id=1)
        token = mcp_compat._REQUEST_CTX.set(ctx)
        try:
            await server.handle_detect_media_silence({"filepath": filepath})
        finally:
            mcp_compat._REQUEST_CTX.reset(token)
        assert session.calls, "no progress emitted for a per-clip scan"
        assert session.calls[-1][0] == session.calls[-1][1]
        assert "silence" in session.calls[0][2]
