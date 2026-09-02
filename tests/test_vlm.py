"""The vision tier never goes online, and says exactly what is missing."""

import os
import socket
import sys
import types
from fractions import Fraction

import pytest

from fcpxml import render, vlm


def test_model_id_from_env(monkeypatch):
    monkeypatch.delenv(vlm.MODEL_ENV, raising=False)
    assert vlm.model_id() == vlm.DEFAULT_MODEL
    monkeypatch.setenv(vlm.MODEL_ENV, "org/other")
    assert vlm.model_id() == "org/other"


def test_model_cached_reads_hub_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert not vlm.model_cached("org/name")
    (tmp_path / "models--org--name" / "snapshots").mkdir(parents=True)
    assert not vlm.model_cached("org/name")  # no snapshot yet
    (tmp_path / "models--org--name" / "snapshots" / "abc").mkdir()
    assert vlm.model_cached("org/name")


def test_load_model_without_extra_names_install(monkeypatch):
    monkeypatch.setattr(vlm, "available", lambda: False)
    vlm._reset()
    with pytest.raises(RuntimeError, match=r"fcp-mcp-server\[find\]"):
        vlm.load_model()


def test_load_model_without_cached_weights_names_download(monkeypatch, tmp_path):
    monkeypatch.setattr(vlm, "available", lambda: True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    vlm._reset()
    with pytest.raises(RuntimeError, match="hf download org/name"):
        vlm.load_model("org/name")


def _fake_mlx(monkeypatch, seen):
    fake = types.ModuleType("mlx_vlm")

    def load(path):
        seen["offline"] = (os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"))
        seen["loads"] = seen.get("loads", 0) + 1
        return ("model", "proc")

    fake.load = load
    fake.generate = lambda *a, **k: "  a caption\n"
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda p: {}
    prompt = types.ModuleType("mlx_vlm.prompt_utils")
    prompt.apply_chat_template = lambda p, c, q, num_images: q
    for name, mod in (("mlx_vlm", fake), ("mlx_vlm.utils", utils), ("mlx_vlm.prompt_utils", prompt)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(vlm, "available", lambda: True)
    monkeypatch.setattr(vlm, "model_cached", lambda m=None: True)
    return fake


def test_load_sets_offline_before_import_and_loads_once(monkeypatch):
    """The instrument: a fake mlx_vlm that records the env at load time."""
    seen = {}
    _fake_mlx(monkeypatch, seen)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    vlm._reset()
    assert vlm.caption("/tmp/frame.jpg") == "a caption"
    assert seen["offline"] == ("1", "1")
    vlm.caption("/tmp/frame2.jpg")
    assert seen["loads"] == 1


def test_caption_handles_result_object(monkeypatch):
    seen = {}
    fake = _fake_mlx(monkeypatch, seen)
    fake.generate = lambda *a, **k: types.SimpleNamespace(text="from object")
    vlm._reset()
    assert vlm.caption("/tmp/frame.jpg") == "from object"


def test_caption_shots_opens_no_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(vlm, "caption", lambda p, model=None, max_tokens=60: "frame " + os.path.basename(p))

    def fake_frame(src, at, out):
        open(out, "wb").write(b"jpg")
        return out

    monkeypatch.setattr(render, "render_frame", fake_frame)
    monkeypatch.setattr(render, "cache_dir", lambda: tmp_path)

    def boom(*a, **k):
        raise AssertionError("socket opened during indexing")

    monkeypatch.setattr(socket, "socket", boom)
    ticks = []
    rows = vlm.caption_shots(
        str(tmp_path / "m.mov"), [(Fraction(0), Fraction(2)), (Fraction(2), Fraction(4))],
        max_frames=1, progress=lambda n, total: ticks.append((n, total)),
    )
    assert len(rows) == 1 and rows[0]["caption"].startswith("frame") and rows[0]["end"] == Fraction(2)
    assert ticks == [(1, 1)]
    assert not list((tmp_path / "frames").glob("*.jpg"))  # frames cleaned up


def test_caption_shots_skips_unrenderable(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "render_frame", lambda src, at, out: None)
    monkeypatch.setattr(render, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(vlm, "caption", lambda *a, **k: pytest.fail("captioned a missing frame"))
    assert vlm.caption_shots(str(tmp_path / "m.mov"), [(Fraction(0), Fraction(1))]) == []
