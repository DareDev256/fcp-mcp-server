"""Generative fill: the price table, the money gate, the key guards, the
provider adapters against a fake transport, the probe, and the insert.

The two guards that matter most — keys never leave the auth header, and
nothing is generated without a confirmed quote — are each paired with a
mutation check: the guard is removed and the test proves it would have let
the failure through. A guard nothing can see fail is not a guard.
"""

import json
import shutil
import subprocess
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml import dtd, gen
from fcpxml.parser import FCPXMLParser
from fcpxml.writer import FCPXMLModifier

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
KEY = "fal-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for env in gen.KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv(gen.MAX_USD_ENV, raising=False)
    gen._QUOTES.clear()
    monkeypatch.setattr(gen, "_sleep", lambda s: None)


# ---------------------------------------------------------------------------
# Prices and quotes
# ---------------------------------------------------------------------------

def test_price_table_is_dated_and_typed():
    assert gen.PRICES_AS_OF == "2026-10-09"
    for (provider, model), entry in gen.PRICES.items():
        assert provider in gen.DEFAULT_MODEL, provider
        assert entry["usd_per_second"] is None or isinstance(entry["usd_per_second"], Decimal), model
        assert isinstance(entry["billable"], tuple)


@pytest.mark.parametrize("provider,model,seconds,billable,usd", [
    ("fal", "kling-2.5", 7, 10, "0.70"),
    ("fal", "kling-2.5", "3.2", 5, "0.35"),
    ("fal", "wan-2.5", 5, 5, "0.75"),
    ("gemini", "veo-3.1", 5, 6, "2.40"),
    ("runway", "aleph", "2.5", 3, "0.45"),
])
def test_quote_bills_the_next_supported_length(provider, model, seconds, billable, usd):
    q = gen.quote(provider, seconds, "a street at dusk", model)
    assert q.billable_seconds == billable
    assert q.usd == Decimal(usd)
    assert q.prices_as_of == gen.PRICES_AS_OF
    assert q.as_dict()["price_verified"] is False
    assert q.as_dict()["estimated_usd"] == usd


def test_quote_over_the_provider_maximum_is_refused():
    with pytest.raises(gen.RefusedError, match="at most 10s"):
        gen.quote("fal", 11, "x")


def test_quote_id_is_bound_to_the_job():
    a = gen.quote("fal", 5, "dusk")
    b = gen.quote("fal", 5, "dusk")
    c = gen.quote("fal", 5, "dawn")
    d = gen.quote("fal", 10, "dusk")
    assert a.id == b.id and a.id != c.id and a.id != d.id


def test_luma_has_no_price_and_is_not_available():
    q = gen.quote("luma", 5, "x")
    assert q.usd is None and q.available is False and q.over_cap is False


def test_unknown_provider_and_model_are_refused_by_name():
    with pytest.raises(gen.RefusedError, match="Providers: fal, gemini"):
        gen.quote("sora", 5, "x")
    with pytest.raises(gen.RefusedError, match="Models: kling-2.5, wan-2.5"):
        gen.quote("fal", 5, "x", "kling-9")


def test_quote_rejects_bad_seconds_and_empty_prompt():
    with pytest.raises(gen.RefusedError, match="seconds must be a number"):
        gen.quote("fal", "five", "x")
    with pytest.raises(gen.RefusedError, match="between 0 and"):
        gen.quote("fal", 0, "x")
    with pytest.raises(gen.RefusedError, match="prompt is required"):
        gen.quote("fal", 5, "  ")


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------

def test_cap_defaults_to_five_and_reads_the_env(monkeypatch):
    assert gen.cap_usd() == Decimal("5")
    monkeypatch.setenv(gen.MAX_USD_ENV, "0.50")
    assert gen.cap_usd() == Decimal("0.50")
    assert gen.quote("fal", 5, "x").over_cap is False   # $0.35
    assert gen.quote("fal", 10, "x").over_cap is True   # $0.70


def test_a_malformed_cap_raises_rather_than_defaulting(monkeypatch):
    monkeypatch.setenv(gen.MAX_USD_ENV, "$4")
    with pytest.raises(ValueError, match=gen.MAX_USD_ENV):
        gen.cap_usd()
    monkeypatch.setenv(gen.MAX_USD_ENV, "-1")
    with pytest.raises(ValueError, match="negative"):
        gen.cap_usd()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_gate_refuses_without_confirm_and_names_the_quote_call():
    with pytest.raises(gen.RefusedError) as exc:
        gen.confirm_quote(None, None, "fal", 7, "dusk")
    text = str(exc.value)
    assert "confirm=true" in text and '"action": "gen_quote"' in text and "$0.70" in text


def test_gate_refuses_confirm_as_a_string():
    q = gen.quote("fal", 7, "dusk")
    with pytest.raises(gen.RefusedError, match="confirm=true"):
        gen.confirm_quote(q.id, "true", "fal", 7, "dusk")


def test_gate_refuses_an_unknown_quote_and_an_expired_one(monkeypatch):
    with pytest.raises(gen.RefusedError, match="no quote_id"):
        gen.confirm_quote(None, True, "fal", 7, "dusk")
    with pytest.raises(gen.RefusedError, match="unknown or older"):
        gen.confirm_quote("deadbeef0000", True, "fal", 7, "dusk")
    q = gen.quote("fal", 7, "dusk")
    monkeypatch.setattr(gen.time, "time", lambda: q.created + gen.QUOTE_TTL_SECONDS + 1)
    with pytest.raises(gen.RefusedError, match="older than 60 minutes"):
        gen.confirm_quote(q.id, True, "fal", 7, "dusk")


def test_gate_refuses_a_quote_for_a_different_job():
    q = gen.quote("fal", 7, "dusk")
    for args in (("fal", 7, "dawn"), ("fal", 3, "dusk"), ("gemini", 7, "dusk"), ("fal", 7, "dusk", "wan-2.5")):
        with pytest.raises(gen.RefusedError, match="different job"):
            gen.confirm_quote(q.id, True, *args)
    # 7s and 10s on Kling both bill 10s: the same job, the same quote.
    assert gen.confirm_quote(q.id, True, "fal", 10, "dusk").id == q.id


def test_gate_refuses_over_cap_and_unimplemented_providers(monkeypatch):
    monkeypatch.setenv(gen.MAX_USD_ENV, "0.5")
    q = gen.quote("fal", 10, "dusk")
    with pytest.raises(gen.RefusedError, match="over the per-call cap"):
        gen.confirm_quote(q.id, True, "fal", 10, "dusk")
    q = gen.quote("runway", 3, "dusk")
    with pytest.raises(gen.RefusedError, match="not implemented"):
        gen.confirm_quote(q.id, True, "runway", 3, "dusk")


def test_gate_passes_a_matching_confirmed_quote():
    q = gen.quote("fal", 7, "dusk")
    ok = gen.confirm_quote(q.id, True, "fal", 7, "dusk")
    assert ok.id == q.id and ok.billable_seconds == 10


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_missing_key_is_refused_by_env_name():
    with pytest.raises(gen.RefusedError, match="FAL_KEY is not set"):
        gen.key_for("fal")


def test_redact_replaces_every_present_key(monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)
    monkeypatch.setenv("GEMINI_API_KEY", "gm-9876543210")
    out = gen.redact(f"boom {KEY} and gm-9876543210 and fine")
    assert KEY not in out and "gm-9876543210" not in out
    assert "[REDACTED:FAL_KEY]" in out and "[REDACTED:GEMINI_API_KEY]" in out


def test_transport_refuses_a_key_in_the_url_or_body(monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)
    sent = []
    monkeypatch.setattr(gen.urllib.request, "urlopen", lambda *a, **k: sent.append(a) or pytest.fail("sent"))
    with pytest.raises(RuntimeError, match="anywhere but a header"):
        gen._http("GET", f"https://example.test/?key={KEY}", {})
    with pytest.raises(RuntimeError, match="anywhere but a header"):
        gen._http("POST", "https://example.test/", {}, json.dumps({"k": KEY}).encode())
    assert sent == []


def test_transport_guard_is_load_bearing(monkeypatch):
    """Mutation check: with the guard removed the key WOULD go out in the URL."""
    monkeypatch.setenv("FAL_KEY", KEY)
    monkeypatch.setattr(gen, "_assert_key_not_outside_headers", lambda url, body: None)
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(gen.urllib.request, "urlopen", fake_urlopen)
    gen._http("GET", f"https://example.test/?key={KEY}", {})
    assert KEY in seen["url"]


# ---------------------------------------------------------------------------
# Adapters against a fake transport
# ---------------------------------------------------------------------------

class _Transport:
    """Records every request and plays back a scripted sequence of replies."""

    def __init__(self, replies):
        self.calls = []
        self.replies = list(replies)

    def __call__(self, method, url, headers, body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        status, payload = self.replies.pop(0)
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def test_fal_adapter_sends_the_key_only_to_the_queue_host(monkeypatch, tmp_path):
    monkeypatch.setenv("FAL_KEY", KEY)
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpegbytes")
    t = _Transport([
        (200, {"request_id": "r1", "status_url": "https://queue.fal.run/s/r1/status",
               "response_url": "https://queue.fal.run/s/r1"}),
        (200, {"status": "IN_QUEUE"}),
        (200, {"status": "COMPLETED"}),
        (200, {"video": {"url": "https://v3.fal.media/files/out.mp4"}}),
        (200, b"MP4BYTES"),
    ])
    monkeypatch.setattr(gen, "_http", t)
    data = gen.FalProvider("kling-2.5").generate("dusk", 10, image_path=str(frame))
    assert data == b"MP4BYTES"
    submit, *polls, download = t.calls
    assert submit["url"] == "https://queue.fal.run/" + gen.PRICES[("fal", "kling-2.5")]["endpoint"]
    assert submit["headers"]["Authorization"] == f"Key {KEY}"
    body = json.loads(submit["body"])
    assert body["prompt"] == "dusk" and body["duration"] == "10"
    assert body["image_url"].startswith("data:image/jpeg;base64,")
    for call in polls:
        assert call["headers"]["Authorization"] == f"Key {KEY}"
        assert call["url"].startswith("https://queue.fal.run/")
    assert download["url"] == "https://v3.fal.media/files/out.mp4"
    assert "Authorization" not in download["headers"]
    assert all(KEY not in c["url"] and KEY not in (c["body"] or b"").decode() for c in t.calls)


def test_fal_text_to_video_when_no_frame(monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)
    t = _Transport([
        (200, {"request_id": "r1"}), (200, {"status": "COMPLETED"}),
        (200, {"video": {"url": "https://v3.fal.media/x.mp4"}}), (200, b"V"),
    ])
    monkeypatch.setattr(gen, "_http", t)
    gen.FalProvider("wan-2.5").generate("dusk", 5)
    assert t.calls[0]["url"].endswith(gen.PRICES[("fal", "wan-2.5")]["text_endpoint"])
    assert "image_url" not in json.loads(t.calls[0]["body"])


def test_fal_failure_and_http_error_become_provider_errors(monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)
    monkeypatch.setattr(gen, "_http", _Transport([(200, {"request_id": "r"}), (200, {"status": "FAILED"})]))
    with pytest.raises(gen.ProviderError, match="failed"):
        gen.FalProvider("kling-2.5").generate("x", 5)
    monkeypatch.setattr(gen, "_http", _Transport([(401, {"detail": f"bad key {KEY}"})]))
    with pytest.raises(gen.ProviderError) as exc:
        gen.FalProvider("kling-2.5").generate("x", 5)
    assert "HTTP 401" in str(exc.value) and KEY not in str(exc.value)


def test_gemini_adapter_uses_the_goog_header_and_polls_the_operation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key-0123456789")
    base = gen.GeminiProvider.BASE
    t = _Transport([
        (200, {"name": "operations/abc"}),
        (200, {"done": False}),
        (200, {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [
            {"video": {"uri": base + "files/v1:download?alt=media"}}]}}}),
        (200, b"VEO"),
    ])
    monkeypatch.setattr(gen, "_http", t)
    data = gen.GeminiProvider("veo-3.1").generate("dusk", 8)
    assert data == b"VEO"
    submit, poll1, poll2, download = t.calls
    assert submit["url"] == base + gen.PRICES[("gemini", "veo-3.1")]["endpoint"]
    assert json.loads(submit["body"]) == {"instances": [{"prompt": "dusk"}], "parameters": {"durationSeconds": 8}}
    for call in t.calls:
        assert call["headers"]["x-goog-api-key"] == "gm-key-0123456789"
        assert call["url"].startswith(base)
    assert poll1["url"] == base + "operations/abc"


def test_gemini_refuses_to_send_the_key_off_googleapis(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key-0123456789")
    t = _Transport([
        (200, {"name": "operations/abc"}),
        (200, {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [
            {"video": {"uri": "https://evil.example/steal"}}]}}}),
    ])
    monkeypatch.setattr(gen, "_http", t)
    with pytest.raises(gen.ProviderError, match="refusing to send the key"):
        gen.GeminiProvider("veo-3.1").generate("dusk", 8)
    assert len(t.calls) == 2


def test_runway_and_luma_are_honest_stubs():
    for provider in ("runway", "luma"):
        with pytest.raises(gen.RefusedError, match="not implemented"):
            gen.provider_for(provider).generate("x", 5)
    assert isinstance(gen.provider_for("fal"), gen.FalProvider)
    assert isinstance(gen.provider_for("gemini", "veo-3.1"), gen.GeminiProvider)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

def _synth(path: Path, rate: int = 30, seconds: int = 2, audio: bool = False) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s=160x90:r={rate}:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440", "-shortest", "-c:a", "aac"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, timeout=60)
    return path


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_probe_reads_the_real_rate_and_length(tmp_path):
    probe = gen.probe_media(str(_synth(tmp_path / "c.mp4", rate=30, seconds=2)))
    assert probe["fps"] == Fraction(30) and probe["duration"] == Fraction(2)
    assert (probe["width"], probe["height"]) == (160, 90) and probe["has_audio"] is False
    with_audio = gen.probe_media(str(_synth(tmp_path / "a.mp4", audio=True)))
    assert with_audio["has_audio"] is True


def test_probe_never_invents_without_ffprobe(tmp_path, monkeypatch):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not a video")
    assert gen.probe_media(str(f)) is None
    monkeypatch.setattr(gen.shutil, "which", lambda name: None)
    assert gen.probe_media(str(f)) is None
    assert gen.probe_media(str(tmp_path / "missing.mp4")) is None


# ---------------------------------------------------------------------------
# The insert, on a DTD-valid project
# ---------------------------------------------------------------------------

VALID_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="A" uid="A1" start="0s" duration="600s" hasVideo="1" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="file:///Media/A.mov"/>
    </asset>
  </resources>
  <library>
    <event name="E">
      <project name="P">
        <sequence format="r1" duration="20s" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="r2" offset="0s" name="A" start="10s" duration="96/24s" format="r1"/>
            <gap name="Gap" offset="96/24s" start="0s" duration="144/24s"/>
            <asset-clip ref="r2" offset="240/24s" name="B" start="100s" duration="48/24s" format="r1"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

PROBE_30 = {"fps": Fraction(30), "duration": Fraction(6), "width": 1280, "height": 720, "has_audio": False}


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "valid.fcpxml"
    p.write_text(VALID_PROJECT)
    (tmp_path / "gen.mp4").write_bytes(b"x")
    return p


def test_insert_hangs_the_clip_off_the_gap_with_role_marker_and_probed_format(project, tmp_path):
    m = FCPXMLModifier(str(project))
    placed = gen.insert_generated(
        m, media_path=str(tmp_path / "gen.mp4"), probe=PROBE_30, at=Fraction(5), duration=Fraction(4),
        name="Generated gap", prompt="a street at dusk", provider_label="fal/kling-2.5",
    )
    out = str(tmp_path / "valid_generated.fcpxml")
    m.save(out)
    assert placed["host"] == "gap" and placed["gap_added"] is False and placed["conformed"] is False

    text = Path(out).read_text()
    assert 'videoRole="generated"' in text
    assert 'frameDuration="1/30s"' in text and 'width="1280"' in text
    assert "generated: fal/kling-2.5 — a street at dusk" in text
    assert 'name="Generated gap" uid=' in text and 'duration="6s"' in text  # whole seconds collapse

    tl = FCPXMLParser().parse_file(out).primary_timeline
    (cc,) = tl.connected_clips
    assert cc.lane == 1 and cc.role == "generated" and cc.parent_clip_name.startswith("gap")
    assert Fraction(cc.timeline_start._exact_seconds) == Fraction(5)
    assert Fraction(cc.duration._exact_seconds) == Fraction(4)
    assert [mk.name for mk in cc.markers] == ["generated: fal/kling-2.5 — a street at dusk"]

    ok, detail = dtd.validate_against_dtd(out)
    if ok is None:
        pytest.skip(f"DTD validation unavailable: {detail}")
    assert ok is True, detail


def test_insert_snaps_to_the_sequence_frame_grid_and_never_claims_more_than_the_media(project, tmp_path):
    m = FCPXMLModifier(str(project))
    placed = gen.insert_generated(
        m, media_path=str(tmp_path / "gen.mp4"), probe=PROBE_30, at=Fraction(5001, 1000),
        duration=Fraction(9), name="g", prompt="p", provider_label="fal/x",
    )
    assert placed["at"] == Fraction(120, 24)            # 5.001s snapped down to the frame
    assert placed["duration"] == Fraction(6)            # capped at the probed 6s
    assert placed["conformed"] is True


def test_asset_duration_is_written_in_the_media_timebase(project, tmp_path):
    m = FCPXMLModifier(str(project))
    probe = dict(PROBE_30, duration=Fraction(13, 2))
    gen.insert_generated(
        m, media_path=str(tmp_path / "gen.mp4"), probe=probe, at=Fraction(5), duration=Fraction(2),
        name="g", prompt="p", provider_label="fal/x",
    )
    asset = [a for a in m.root.iter("asset") if a.get("id") == "r_gen1"][0]
    assert asset.get("duration") == "195/30s"
    clip = [c for c in m.root.iter("asset-clip") if c.get("ref") == "r_gen1"][0]
    # offset is in the HOST's local clock: the gap starts at 4s, so 5s on the timeline is 1s into it.
    assert clip.get("duration") == "2s" and clip.get("offset") == "1s"


def test_insert_appends_a_gap_past_the_end_of_the_spine(project, tmp_path):
    m = FCPXMLModifier(str(project))
    placed = gen.insert_generated(
        m, media_path=str(tmp_path / "gen.mp4"), probe=PROBE_30, at=Fraction(15), duration=Fraction(3),
        name="g", prompt="p", provider_label="fal/x",
    )
    assert placed["gap_added"] is True and placed["host"] == "gap"
    out = str(tmp_path / "tail.fcpxml")
    m.save(out)
    gaps = [g for g in m.root.iter("gap")]
    assert gaps[-1].get("offset") == "12s" and gaps[-1].get("duration") == "6s"
    tl = FCPXMLParser().parse_file(out).primary_timeline
    assert Fraction(tl.connected_clips[0].timeline_start._exact_seconds) == Fraction(15)


def test_insert_refuses_lane_zero_and_sub_frame_durations(project, tmp_path):
    m = FCPXMLModifier(str(project))
    with pytest.raises(ValueError, match="lane must be non-zero"):
        gen.insert_generated(m, media_path="x", probe=PROBE_30, at=Fraction(5), duration=Fraction(2),
                             name="g", prompt="p", provider_label="x", lane=0)
    with pytest.raises(ValueError, match="under one frame"):
        gen.insert_generated(m, media_path="x", probe=PROBE_30, at=Fraction(5), duration=Fraction(1, 100),
                             name="g", prompt="p", provider_label="x")
