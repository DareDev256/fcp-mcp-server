"""The gen tool group through the MCP surface: wiring, the gate as the model
sees it, the end-to-end insert with a fake provider and real ffmpeg media,
the journal rows, and the key-leak guard with its mutation check."""

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import server
import tools
import tools.gen as gen_group
from fcpxml import gen, journal
from fcpxml.parser import FCPXMLParser

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.fcpxml"
KEY = "fal-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for env in gen.KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv(gen.MAX_USD_ENV, raising=False)
    gen._QUOTES.clear()


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    p = folder / "sample.fcpxml"
    shutil.copy(SAMPLE, p)
    return p


@pytest.fixture
def video(tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    out = tmp_path / "fake.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=30:d=6",
         "-pix_fmt", "yuv420p", str(out)], check=True, timeout=60,
    )
    return out.read_bytes()


class _FakeProvider(gen.Provider):
    name = "fal"
    calls = []

    def __init__(self, model, data=b""):
        super().__init__(model)
        self.data = data

    def generate(self, prompt, seconds, *, image_path=None):
        _FakeProvider.calls.append({"prompt": prompt, "seconds": seconds, "image": image_path})
        return self.data


@pytest.fixture
def fake(monkeypatch, video):
    _FakeProvider.calls = []
    monkeypatch.setenv("FAL_KEY", KEY)
    monkeypatch.setattr(gen, "provider_for", lambda p, m=None: _FakeProvider(gen.resolve_model(p, m), video))
    return _FakeProvider


def _call(action, args):
    return asyncio.run(server.call_tool("gen", {"action": action, "args": args}))[0].text


def _quote(provider="fal", seconds=7, prompt="a street at dusk", model=None):
    args = {"provider": provider, "seconds": seconds, "prompt": prompt}
    if model:
        args["model"] = model
    text = _call("gen_quote", args)
    return re.search(r"Quote (\w+)", text).group(1), text


# ---------------------------------------------------------------------------

def test_gen_group_is_advertised():
    assert tools.EXTRA_GROUPS["gen"]["actions"] == ["gen_quote", "gen_fill_gap", "gen_broll", "gen_extend_clip"]
    assert "gen" in server.TOOL_GROUPS
    assert "meta" not in tools.EXTRA_GROUPS["gen"]


def test_every_gen_action_is_reachable_through_call_tool():
    for action in tools.EXTRA_GROUPS["gen"]["actions"]:
        result = asyncio.run(server.call_tool("gen", {"action": action}))
        assert result and result[0].text


def test_quote_reports_price_date_cap_and_how_to_spend_it():
    qid, text = _quote()
    assert "fal/kling-2.5, 10s billable for 7.00s requested" in text
    assert "$0.70" in text and gen.PRICES_AS_OF in text and "NOT verified" in text
    assert "Cap: $5" in text and f'quote_id="{qid}"' in text and "confirm=true" in text
    assert '"price_verified": false' in text


def test_quote_says_over_cap_and_not_implemented(monkeypatch):
    monkeypatch.setenv(gen.MAX_USD_ENV, "0.5")
    _, text = _quote(seconds=10)
    assert "OVER CAP" in text
    _, text = _quote(provider="luma", seconds=5)
    assert "no price on file" in text and "not implemented" in text


def test_fill_gap_refuses_without_confirm_before_touching_a_key(project, monkeypatch):
    monkeypatch.setattr(gen, "key_for", lambda p: pytest.fail("key read before the gate"))
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "dusk", "seconds": 7})
    assert text.startswith("Refused: generation spends money")
    assert not (project.parent / "generated").exists()
    assert journal.records(str(project)) == []


def test_fill_gap_refuses_a_missing_key_by_name(project):
    qid, _ = _quote()
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at dusk", "seconds": 7,
                                  "quote_id": qid, "confirm": True})
    assert "FAL_KEY is not set" in text


def test_fill_gap_refuses_a_mismatched_quote(project, fake):
    qid, _ = _quote()
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at DAWN", "seconds": 7,
                                  "quote_id": qid, "confirm": True})
    assert "different job" in text and fake.calls == []


def test_fill_gap_refuses_over_cap_without_generating(project, fake, monkeypatch):
    monkeypatch.setenv(gen.MAX_USD_ENV, "0.5")
    qid, _ = _quote(seconds=10)
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at dusk", "seconds": 10,
                                  "quote_id": qid, "confirm": True})
    assert "over the per-call cap" in text and fake.calls == []


def test_a_gap_longer_than_the_provider_allows_says_so(project, fake):
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "dusk"})
    assert "at most 10s" in text and "66.25s" in text and fake.calls == []


def test_fill_gap_generates_probes_inserts_and_journals(project, fake):
    qid, _ = _quote()
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at dusk", "seconds": 7,
                                  "quote_id": qid, "confirm": True})
    assert fake.calls == [{"prompt": "a street at dusk", "seconds": 10, "image": None}]
    assert "Generated 10s on fal/kling-2.5" in text
    assert "@ 30/1 fps, probed" in text  # the file's rate, not the sequence's 24
    assert "role `generated`" in text and 'marker "generated: fal/kling-2.5 — a street at dusk"' in text
    assert "a <gap> was appended" in text
    assert "Estimated cost: $0.70" in text and "DTD:" in text

    media = re.search(r"Media: (\S+\.mp4)", text).group(1)
    assert Path(media).parent == project.parent / "generated"
    out = re.search(r"Saved to: `([^`]+)`", text).group(1)
    assert out.endswith("sample_generated.fcpxml")
    assert f"sha256: {journal.file_hash(out)}" in text

    tl = FCPXMLParser().parse_file(out).primary_timeline
    (cc,) = tl.connected_clips
    assert cc.role == "generated" and float(cc.timeline_start.seconds) == 53.75 and float(cc.duration.seconds) == 6.0

    rows = journal.records(str(project))
    assert {Path(r["output"]["path"]).name for r in rows} == {"sample_generated.fcpxml", Path(media).name}
    assert all(r["action"] == "gen_fill_gap" for r in rows)


def test_extend_clip_places_at_the_out_point_and_says_it_had_no_frame(project, fake):
    qid, _ = _quote(seconds=5, prompt="continue the shot")
    text = _call("gen_extend_clip", {"filepath": str(project), "clip_name": "Broll_City", "seconds": 5,
                                     "quote_id": qid, "confirm": True})
    assert "continuing 'Broll_City' from its out point at 5.00s" in text
    assert "source media not found; generated from the prompt alone" in text
    assert "Placed: lane 1 at 5.000s for 5.000s on a <asset-clip>" in text
    assert fake.calls[0]["image"] is None


def test_broll_conditions_on_a_real_frame_when_the_media_exists(project, fake, tmp_path, monkeypatch):
    media = tmp_path / "Broll_City.mov"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=24:d=12",
         "-pix_fmt", "yuv420p", str(media)], check=True, timeout=60,
    )
    project.write_text(project.read_text().replace("file:///Media/Broll_City.mov", media.as_uri()))
    monkeypatch.setenv("HOME", str(tmp_path))  # the condition frame lands in the private cache
    qid, _ = _quote(seconds=5, prompt="a crowd crossing")
    text = _call("gen_broll", {"filepath": str(project), "clip_name": "Broll_City", "prompt": "a crowd crossing",
                               "seconds": 5, "at": 1, "quote_id": qid, "confirm": True})
    assert "conditioned on the frame at 6.000s of Broll_City.mov" in text  # source in 5s + 1s into the clip
    assert fake.calls[0]["image"] and Path(fake.calls[0]["image"]).is_file()
    assert "Placed: lane 1 at 4.000s" in text  # clip starts at 3s, +1s


def test_unknown_clip_lists_the_names(project, fake):
    qid, _ = _quote(seconds=5)
    text = _call("gen_broll", {"filepath": str(project), "clip_name": "nope", "prompt": "a street at dusk",
                               "quote_id": qid, "confirm": True})
    assert "No clip named 'nope'" in text and "Interview_A" in text


def test_an_unprobeable_result_is_kept_but_not_inserted(project, monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)
    monkeypatch.setattr(gen, "provider_for", lambda p, m=None: _FakeProvider("kling-2.5", b"not a video"))
    qid, _ = _quote()
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at dusk", "seconds": 7,
                                  "quote_id": qid, "confirm": True})
    assert "ffprobe could not read it" in text and "Nothing was inserted" in text
    assert list((project.parent / "generated").glob("*.mp4"))
    assert not (project.parent / "sample_generated.fcpxml").exists()


def test_provider_errors_never_leak_the_key(project, monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEY)

    class _Leaky(gen.Provider):
        name = "fal"

        def generate(self, prompt, seconds, *, image_path=None):
            raise gen.ProviderError(f"upstream said: unauthorized for {KEY}")

    monkeypatch.setattr(gen, "provider_for", lambda p, m=None: _Leaky("kling-2.5"))
    qid, _ = _quote()
    args = {"filepath": str(project), "prompt": "a street at dusk", "seconds": 7, "quote_id": qid, "confirm": True}
    text = _call("gen_fill_gap", args)
    assert "unauthorized" in text and KEY not in text and "[REDACTED:FAL_KEY]" in text

    # Mutation check: without redact the key WOULD be in the reply.
    monkeypatch.setattr(gen, "redact", lambda s: s)
    assert KEY in _call("gen_fill_gap", args)


def test_gen_writes_stay_inside_the_project_folder(project, fake):
    qid, _ = _quote()
    text = _call("gen_fill_gap", {"filepath": str(project), "prompt": "a street at dusk", "seconds": 7,
                                  "quote_id": qid, "confirm": True, "output_path": "/tmp/escape.fcpxml"})
    assert "escapes allowed directory" in text and fake.calls == []


def test_frame_of_reports_missing_media_without_raising():
    frame, note = gen_group._frame_of("file:///Media/none.mov", 1, "t")
    assert frame is None and "not found" in note
