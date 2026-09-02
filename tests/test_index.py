"""The index is a cache. Every test here holds that line.

A cache that is wrong is worse than no cache, so the invariants are: time
values are stored as integer pairs and never as floats; a source file that
changed on disk drops every dependent row; a corrupt or foreign database is
rebuilt rather than trusted; and when disabled, nothing is opened at all.
"""

import os
import sqlite3
from fractions import Fraction

import pytest

from fcpxml import index as idx
from tests.conftest import requires_index


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "shot.mp4"
    p.write_bytes(b"\x00" * 1024)
    return str(p)


class TestDisabled:
    @pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
    def test_open_returns_none(self, monkeypatch, value):
        monkeypatch.setenv("FCP_MCP_INDEX", value)
        assert idx.enabled() is False
        assert idx.index_path() is None
        assert idx.Index.open() is None

    def test_default_path_lives_under_home(self, monkeypatch):
        monkeypatch.delenv("FCP_MCP_INDEX", raising=False)
        assert idx.index_path().name == "index.db"
        assert idx.index_path().parent.name == ".fcp-mcp"


class TestPairs:
    def test_float_becomes_integer_pair(self):
        num, den = idx.to_pair(1.5)
        assert (num, den) == (3, 2)
        assert idx.from_pair(num, den) == Fraction(3, 2)

    def test_ntsc_frame_survives(self):
        num, den = idx.to_pair(Fraction(1001, 30000))
        assert Fraction(num, den) == Fraction(1001, 30000)


@requires_index
class TestRoundTrip:
    def test_analysis_round_trip(self, media):
        with idx.Index.open() as ix:
            assert ix.get_analysis(media, "silence") is None
            ix.put_analysis(media, "silence", [
                {"start": 0.5, "end": 1.25, "payload": {"db": -30}},
            ])
            rows = ix.get_analysis(media, "silence")
        assert rows == [{"start": Fraction(1, 2), "end": Fraction(5, 4), "payload": {"db": -30}}]

    def test_no_float_ever_reaches_a_time_column(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "beat", [{"start": 0.1, "end": 0.1, "payload": None}])
        con = sqlite3.connect(os.environ["FCP_MCP_INDEX"])
        try:
            for table in ("analysis", "transcript", "shot"):
                cols = con.execute(f"PRAGMA table_info({table})").fetchall()
                for _, name, ctype, *_ in cols:
                    if name.endswith(("_num", "_den")):
                        assert ctype.upper() == "INTEGER", (table, name, ctype)
            (start_num, start_den) = con.execute(
                "SELECT start_num, start_den FROM analysis"
            ).fetchone()
            assert isinstance(start_num, int) and isinstance(start_den, int)
        finally:
            con.close()

    def test_transcript_round_trip(self, media):
        data = {
            "language": "en", "duration": 2.0, "text": "hi there",
            "segments": [{"text": "hi there", "start": 0.0, "end": 1.0}],
            "words": [
                {"word": "hi", "start": 0.0, "end": 0.4},
                {"word": "there", "start": 0.5, "end": 1.0, "speaker": "S0"},
            ],
        }
        with idx.Index.open() as ix:
            assert ix.get_transcript(media) is None
            ix.put_transcript(media, data)
            back = ix.get_transcript(media)
        assert back["text"] == "hi there"
        assert back["language"] == "en"
        assert [w["word"] for w in back["words"]] == ["hi", "there"]
        assert back["words"][1]["speaker"] == "S0"
        assert back["words"][0]["speaker"] is None

    def test_empty_list_is_a_hit_not_a_miss(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
            assert ix.get_analysis(media, "silence") == []


@requires_index
class TestInvalidation:
    def test_a_touched_source_drops_its_rows(self, media):
        """Mutation target: remove the (mtime,size) check in media_id and this goes red."""
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [{"start": 0, "end": 1, "payload": None}])
        st = os.stat(media)
        os.utime(media, (st.st_atime, st.st_mtime + 5))
        with idx.Index.open() as ix:
            assert ix.get_analysis(media, "silence") is None

    def test_a_resized_source_drops_its_rows(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [{"start": 0, "end": 1, "payload": None}])
        st = os.stat(media)
        with open(media, "ab") as f:
            f.write(b"\x00")
        os.utime(media, (st.st_atime, st.st_mtime))
        with idx.Index.open() as ix:
            assert ix.get_analysis(media, "silence") is None

    def test_a_missing_source_is_a_miss(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
        os.remove(media)
        with idx.Index.open() as ix:
            assert ix.get_analysis(media, "silence") is None


@requires_index
class TestRebuild:
    def test_garbage_file_is_rebuilt(self, media):
        path = os.environ["FCP_MCP_INDEX"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"not a database at all")
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
            assert ix.get_analysis(media, "silence") == []

    def test_schema_drift_is_rebuilt(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
        con = sqlite3.connect(os.environ["FCP_MCP_INDEX"])
        con.execute("PRAGMA user_version = 999")
        con.commit()
        con.close()
        with idx.Index.open() as ix:
            assert ix.get_analysis(media, "silence") is None
            assert ix.stats()["media"] == 0

    def test_directory_is_private(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
        mode = os.stat(os.path.dirname(os.environ["FCP_MCP_INDEX"])).st_mode & 0o777
        assert mode == 0o700


@requires_index
class TestStats:
    def test_counts_and_age(self, media):
        with idx.Index.open() as ix:
            assert ix.stats() == {"media": 0, "transcript": 0, "analysis": 0, "shot": 0, "oldest_age": None}
            ix.put_analysis(media, "silence", [{"start": 0, "end": 1, "payload": None}])
            ix.put_transcript(media, {"text": "x", "words": [{"word": "x", "start": 0, "end": 1}], "segments": []})
            s = ix.stats()
        assert (s["media"], s["transcript"], s["analysis"]) == (1, 1, 1)
        assert s["oldest_age"] is not None and s["oldest_age"] >= 0

    def test_clear(self, media):
        with idx.Index.open() as ix:
            ix.put_analysis(media, "silence", [])
            ix.clear()
            assert ix.stats()["media"] == 0


@requires_index
def test_shots_round_trip_and_invalidate(tmp_path):
    media = tmp_path / "m.mov"
    media.write_bytes(b"x" * 10)
    with idx.Index.open() as ix:
        assert ix.get_shots(str(media)) is None
        ix.put_shots(str(media), [{"start": 0.0, "end": 1.5, "caption": "a man"}])
        assert ix.get_shots(str(media)) == [
            {"start": Fraction(0), "end": Fraction(3, 2), "caption": "a man"}
        ]
        assert ix.stats()["shot"] == 1
    media.write_bytes(b"y" * 11)
    with idx.Index.open() as ix:
        assert ix.get_shots(str(media)) is None
