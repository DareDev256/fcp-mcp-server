"""index_status / index_build / index_clear through the MCP seam."""

import pytest

import server
from tests.conftest import requires_index
from tests.test_media_intel import PROJECT_XML


@pytest.fixture
def project(tmp_path):
    media = tmp_path / "interview.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    path = tmp_path / "project.fcpxml"
    path.write_text(PROJECT_XML.format(src=f"file://{media}"))
    return str(path), str(media)


async def _call(action, args):
    result = await server.call_tool("index", {"action": action, "args": args})
    return result[0].text


class TestDisabled:
    async def test_every_action_says_so(self, monkeypatch, project):
        monkeypatch.setenv("FCP_MCP_INDEX", "off")
        for action in ("index_status", "index_build", "index_clear"):
            text = await _call(action, {"filepath": project[0]})
            assert "FCP_MCP_INDEX=off" in text
            assert "still work" in text


@requires_index
class TestStatus:
    async def test_empty_then_populated(self, project, monkeypatch):
        filepath, media = project
        text = await _call("index_status", {})
        assert "**Media rows**: 0" in text and "empty" in text
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: [(1.0, 2.0)])
        await server.handle_detect_media_silence({"filepath": filepath})
        text = await _call("index_status", {})
        assert "**Media rows**: 1" in text
        assert "**Analysis rows**: 1" in text
        assert "**Oldest entry**: empty" not in text


@requires_index
class TestBuild:
    async def test_warms_silence_and_reports_missing(self, project, monkeypatch, tmp_path):
        filepath, media = project
        calls = []
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: calls.append(p) or [(0.0, 0.5)])
        monkeypatch.setattr("tools.index.probe_duration", lambda p: None)
        text = await _call("index_build", {"filepath": filepath})
        assert "1 indexed" in text and "interview.wav" in text
        assert calls == [media]
        # The warm-up did the work: the real tool now answers from cache.
        await server.handle_detect_media_silence({"filepath": filepath})
        assert len(calls) == 1

    async def test_with_transcript(self, project, monkeypatch):
        filepath, media = project
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: [])
        monkeypatch.setattr("tools.index.probe_duration", lambda p: None)
        monkeypatch.setattr(
            server, "transcribe",
            lambda p, model_size, language, backend="local": {"language": "en", "duration": 1.0, "text": "hi",
                                             "segments": [], "words": [{"word": "hi", "start": 0, "end": 1}]},
        )
        text = await _call("index_build", {"filepath": filepath, "with_transcript": True})
        assert "| yes |" in text

    async def test_missing_media_is_listed(self, tmp_path):
        path = tmp_path / "p.fcpxml"
        path.write_text(PROJECT_XML.format(src="file:///nope/interview.wav"))
        text = await _call("index_build", {"filepath": str(path)})
        assert "1 missing" in text and "interview.wav" in text


@requires_index
class TestClear:
    async def test_clear_drops_rows(self, project, monkeypatch):
        filepath, media = project
        monkeypatch.setattr(server, "detect_silence", lambda p, **kw: [])
        await server.handle_detect_media_silence({"filepath": filepath})
        text = await _call("index_clear", {})
        assert "1 media rows dropped" in text
        assert "**Media rows**: 0" in await _call("index_status", {})
