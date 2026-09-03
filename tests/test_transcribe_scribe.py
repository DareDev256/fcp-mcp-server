"""The ElevenLabs Scribe backend — opt-in, and the audio leaves the machine.

Everything here runs with urllib monkeypatched. No test in this file makes
a network call; a test that does is a defect, not coverage.
"""

import io
import json
from pathlib import Path

import pytest

from fcpxml import transcribe as tr
from tests.test_media_intel import PROJECT_XML

SCRIBE_RESPONSE = {
    "language_code": "eng",
    "language_probability": 0.99,
    "text": "Hello world [laughter] yes",
    "audio_duration_secs": 3.0,
    "words": [
        {"text": "Hello", "type": "word", "start": 0.0, "end": 0.4, "speaker_id": "speaker_1", "logprob": 0.0},
        {"text": " ", "type": "spacing", "start": 0.4, "end": 0.5, "speaker_id": "speaker_1", "logprob": 0.0},
        {"text": "world", "type": "word", "start": 0.5, "end": 0.9, "speaker_id": "speaker_1", "logprob": 0.0},
        {"text": "(laughter)", "type": "audio_event", "start": 1.0, "end": 1.8, "speaker_id": None, "logprob": 0.0},
        {"text": "yes", "type": "word", "start": 2.0, "end": 2.3, "speaker_id": "speaker_0", "logprob": 0.0},
    ],
}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "talk.wav"
    p.write_bytes(b"RIFF" + b"\x00" * 64)
    return str(p)


@pytest.fixture
def captured(monkeypatch):
    """Route urlopen into a recorder that returns SCRIBE_RESPONSE."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "headers": dict(req.header_items()),
                      "body": req.data, "timeout": timeout})
        return _Resp(json.dumps(SCRIBE_RESPONSE).encode())

    monkeypatch.setattr(tr.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-secret-value")
    return calls


class TestMapping:
    def test_words_gain_speakers_and_events_are_split_out(self, media, captured):
        out = tr.transcribe(media, backend="elevenlabs")
        assert out["backend"] == "elevenlabs"
        assert out["language"] == "eng"
        assert out["duration"] == 3.0
        assert [w["word"] for w in out["words"]] == ["Hello", "world", "yes"]
        # First speaker heard is S0, regardless of the provider's numbering.
        assert [w["speaker"] for w in out["words"]] == ["S0", "S0", "S1"]
        assert out["events"] == [{"start": 1.0, "end": 1.8, "label": "laughter"}]
        assert out["segments"] and out["segments"][0]["text"] == "Hello world"
        assert out["text"] == "Hello world [laughter] yes"

    def test_request_shape(self, media, captured):
        tr.transcribe(media, backend="elevenlabs", language="en")
        (call,) = captured
        assert call["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
        assert call["timeout"] == tr.SCRIBE_TIMEOUT_SECONDS
        body = call["body"]
        assert b'name="model_id"\r\n\r\nscribe_v2' in body
        assert b'name="diarize"\r\n\r\ntrue' in body
        assert b'name="tag_audio_events"\r\n\r\ntrue' in body
        assert b'name="timestamps_granularity"\r\n\r\nword' in body
        assert b'name="language_code"\r\n\r\nen' in body
        assert b'filename="talk.wav"' in body
        assert b"RIFF" in body


class TestTheKeyStaysOut:
    def test_key_goes_in_the_header_and_nowhere_else(self, media, captured):
        out = tr.transcribe(media, backend="elevenlabs")
        (call,) = captured
        assert call["headers"].get("Xi-api-key") == "xi-test-secret-value"
        assert "xi-test-secret-value" not in call["url"]
        assert b"xi-test-secret-value" not in call["body"]
        assert "xi-test-secret-value" not in json.dumps(out)

    def test_missing_key_is_none_not_a_request(self, media, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.setattr(tr.urllib.request, "urlopen",
                            lambda *a, **k: pytest.fail("network call without a key"))
        assert tr.transcribe(media, backend="elevenlabs") is None

    def test_http_failure_is_none(self, media, captured, monkeypatch):
        def boom(req, timeout=None):
            raise tr.urllib.error.URLError("no route")
        monkeypatch.setattr(tr.urllib.request, "urlopen", boom)
        assert tr.transcribe(media, backend="elevenlabs") is None

    def test_oversized_body_is_refused(self, media, captured, monkeypatch):
        monkeypatch.setattr(tr, "SCRIBE_MAX_RESPONSE_BYTES", 10)
        assert tr.transcribe(media, backend="elevenlabs") is None


class TestBackendArg:
    def test_bogus_backend_raises(self, media):
        with pytest.raises(ValueError, match="backend"):
            tr.transcribe(media, backend="bogus")

    def test_local_is_untouched(self, media, monkeypatch):
        monkeypatch.setattr(tr.urllib.request, "urlopen",
                            lambda *a, **k: pytest.fail("local backend hit the network"))
        # No faster-whisper in the floor venv: None, exactly as before.
        assert tr.transcribe(media, backend="local") is None

    def test_missing_file_is_none_before_any_request(self, tmp_path, captured):
        assert tr.transcribe(str(tmp_path / "nope.wav"), backend="elevenlabs") is None
        assert captured == []


class TestThroughTheServer:
    @pytest.fixture
    def project(self, tmp_path, media):
        path = tmp_path / "project.fcpxml"
        path.write_text(PROJECT_XML.format(src=f"file://{media}"))
        return str(path)

    async def test_result_says_audio_left_the_machine(self, project, captured):
        import server
        out = await server.handle_transcribe_media({"filepath": project, "backend": "elevenlabs"})
        text = out[0].text
        assert "Audio left this machine" in text
        assert "api.elevenlabs.io" in text
        assert "xi-test-secret-value" not in text

    async def test_local_result_does_not_claim_egress(self, project, media, captured):
        import server
        Path(media).with_name("talk_transcript.json").write_text(json.dumps(
            {"words": [{"word": "hi", "start": 0.0, "end": 0.2}], "segments": [], "text": "hi"}))
        out = await server.handle_transcribe_media({"filepath": project})
        assert "Audio left this machine" not in out[0].text

    async def test_a_local_cache_does_not_satisfy_a_diarize_request(self, project, media, captured):
        import server
        Path(media).with_name("talk_transcript.json").write_text(json.dumps(
            {"words": [{"word": "hi", "start": 0.0, "end": 0.2}], "segments": [], "text": "hi"}))
        await server.handle_transcribe_media({"filepath": project, "backend": "elevenlabs"})
        assert len(captured) == 1
        # ...and now the diarized sidecar satisfies both.
        await server.handle_transcribe_media({"filepath": project, "backend": "elevenlabs"})
        await server.handle_transcribe_media({"filepath": project})
        assert len(captured) == 1

    async def test_missing_key_reports_what_to_set(self, project, monkeypatch):
        import server
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        out = await server.handle_transcribe_media({"filepath": project, "backend": "elevenlabs"})
        assert "ELEVENLABS_API_KEY" in out[0].text

    async def test_pack_carries_speakers_from_scribe(self, project, captured):
        import server
        out = await server.call_tool("transcript", {"action": "transcript_pack",
                                                    "args": {"filepath": project, "backend": "elevenlabs"}})
        text = out[0].text
        assert "S0 Hello world" in text
        assert "(laughter)" in text
        assert "S1 yes" in text
