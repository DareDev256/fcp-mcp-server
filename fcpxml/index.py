"""The analysis index — a SQLite cache under ``~/.fcp-mcp/index.db``.

This is a cache and never a source of truth. Every tool that reads from it
must produce the same answer with the database deleted or with
``FCP_MCP_INDEX=off``, only more slowly. The suite is run under both
conditions to keep that promise honest.

Three invariants, each with a test:

* Time values are stored as integer ``num/den`` pairs. A float from ffmpeg or
  whisper is converted once, at the boundary, with ``limit_denominator`` —
  the same rule the rest of the codebase applies to rational time.
* Rows are keyed to a source by ``(path, mtime, size)``. A source that changed
  on disk drops every dependent row on the next touch. Stale analysis of a
  re-exported file is the failure nobody notices, so it is refused here.
* A corrupt or foreign database is dropped and rebuilt. There is nothing in it
  that cannot be recomputed.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_OFF = {"off", "0", "false", "no"}
_DENOMINATOR_CAP = 1_000_000

_SCHEMA = """
CREATE TABLE media (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  duration_num INTEGER, duration_den INTEGER,
  fps_num INTEGER, fps_den INTEGER,
  width INTEGER, height INTEGER,
  indexed_at REAL NOT NULL
);
CREATE TABLE transcript (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  text TEXT NOT NULL,
  speaker TEXT
);
CREATE TABLE transcript_meta (
  media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
  language TEXT,
  duration_num INTEGER, duration_den INTEGER,
  text TEXT NOT NULL,
  segments TEXT NOT NULL,
  events TEXT NOT NULL
);
CREATE TABLE analysis (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  payload TEXT
);
CREATE TABLE analysis_done (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  done_at REAL NOT NULL,
  PRIMARY KEY (media_id, kind)
);
CREATE TABLE shot (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  caption TEXT,
  embedding BLOB
);
CREATE INDEX analysis_by_media ON analysis(media_id, kind);
CREATE INDEX transcript_by_media ON transcript(media_id);
"""


def index_path() -> Optional[Path]:
    """Where the database lives, or ``None`` when the index is switched off."""
    raw = os.environ.get("FCP_MCP_INDEX", "").strip()
    if raw.lower() in _OFF:
        return None
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".fcp-mcp" / "index.db"


def enabled() -> bool:
    return index_path() is not None


def to_pair(seconds: Any) -> tuple[int, int]:
    """Convert seconds (float, int, Fraction) to an integer ``(num, den)``."""
    if isinstance(seconds, Fraction):
        frac = seconds
    else:
        frac = Fraction(seconds).limit_denominator(_DENOMINATOR_CAP)
    return frac.numerator, frac.denominator


def from_pair(num: Optional[int], den: Optional[int]) -> Optional[Fraction]:
    if num is None or den is None or den == 0:
        return None
    return Fraction(num, den)


class Index:
    """One open connection to the cache. Use as a context manager."""

    def __init__(self, con: sqlite3.Connection, path: Path):
        self._con = con
        self.path = path

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls) -> Optional["Index"]:
        path = index_path()
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        con = cls._connect(path)
        if con is None:
            # Corrupt, foreign, or from another schema version: rebuild.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            con = cls._connect(path)
            if con is None:  # pragma: no cover - disk is unwritable
                return None
        return cls(con, path)

    @classmethod
    def _connect(cls, path: Path) -> Optional[sqlite3.Connection]:
        try:
            con = sqlite3.connect(str(path))
            con.execute("PRAGMA foreign_keys = ON")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                con.executescript(_SCHEMA)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.commit()
            elif version != SCHEMA_VERSION:
                con.close()
                logger.info("index schema %s != %s; rebuilding", version, SCHEMA_VERSION)
                return None
            # A quick integrity probe — a truncated file passes connect() but
            # fails its first real read.
            con.execute("SELECT count(*) FROM media").fetchone()
            return con
        except sqlite3.DatabaseError as exc:
            logger.info("index unreadable (%s); rebuilding", exc)
            try:
                con.close()
            except Exception:
                pass
            return None

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- media rows -----------------------------------------------------------

    def media_id(self, path: str, create: bool = False) -> Optional[int]:
        """Row id for *path* if its ``(mtime, size)`` still match the disk.

        A stale row (source changed) is deleted, cascading every dependent
        row. Returns ``None`` when the file is missing or — unless *create* —
        when no fresh row exists.
        """
        try:
            st = os.stat(path)
        except OSError:
            return None
        row = self._con.execute(
            "SELECT id, mtime, size FROM media WHERE path = ?", (path,)
        ).fetchone()
        if row is not None:
            row_id, mtime, size = row
            if mtime == st.st_mtime and size == st.st_size:
                return row_id
            self._con.execute("DELETE FROM media WHERE id = ?", (row_id,))
            self._con.commit()
        if not create:
            return None
        cur = self._con.execute(
            "INSERT INTO media (path, mtime, size, indexed_at) VALUES (?, ?, ?, ?)",
            (path, st.st_mtime, st.st_size, time.time()),
        )
        self._con.commit()
        return cur.lastrowid

    # -- analysis ---------------------------------------------------------------

    def get_analysis(self, path: str, kind: str) -> Optional[list[dict]]:
        mid = self.media_id(path)
        if mid is None:
            return None
        done = self._con.execute(
            "SELECT 1 FROM analysis_done WHERE media_id = ? AND kind = ?", (mid, kind)
        ).fetchone()
        if done is None:
            return None
        rows = self._con.execute(
            "SELECT start_num, start_den, end_num, end_den, payload FROM analysis "
            "WHERE media_id = ? AND kind = ? ORDER BY rowid",
            (mid, kind),
        ).fetchall()
        return [
            {
                "start": from_pair(sn, sd),
                "end": from_pair(en, ed),
                "payload": json.loads(payload) if payload is not None else None,
            }
            for sn, sd, en, ed, payload in rows
        ]

    def put_analysis(self, path: str, kind: str, rows: list[dict]) -> None:
        mid = self.media_id(path, create=True)
        if mid is None:
            return
        self._con.execute("DELETE FROM analysis WHERE media_id = ? AND kind = ?", (mid, kind))
        self._con.executemany(
            "INSERT INTO analysis (media_id, kind, start_num, start_den, end_num, end_den, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (mid, kind, *to_pair(r["start"]), *to_pair(r["end"]),
                 json.dumps(r.get("payload")) if r.get("payload") is not None else None)
                for r in rows
            ],
        )
        self._con.execute(
            "INSERT OR REPLACE INTO analysis_done (media_id, kind, done_at) VALUES (?, ?, ?)",
            (mid, kind, time.time()),
        )
        self._con.commit()

    # -- transcripts -------------------------------------------------------------

    def get_transcript(self, path: str) -> Optional[dict]:
        mid = self.media_id(path)
        if mid is None:
            return None
        meta = self._con.execute(
            "SELECT language, duration_num, duration_den, text, segments, events "
            "FROM transcript_meta WHERE media_id = ?", (mid,)
        ).fetchone()
        if meta is None:
            return None
        language, dn, dd, text, segments, events = meta
        words = self._con.execute(
            "SELECT start_num, start_den, end_num, end_den, text, speaker FROM transcript "
            "WHERE media_id = ? ORDER BY rowid", (mid,)
        ).fetchall()
        duration = from_pair(dn, dd)
        return {
            "language": language,
            "duration": float(duration) if duration is not None else 0.0,
            "text": text,
            "segments": json.loads(segments),
            "events": json.loads(events),
            "words": [
                {
                    "word": w,
                    "start": float(from_pair(sn, sd)),
                    "end": float(from_pair(en, ed)),
                    "speaker": speaker,
                }
                for sn, sd, en, ed, w, speaker in words
            ],
        }

    def put_transcript(self, path: str, data: dict) -> None:
        mid = self.media_id(path, create=True)
        if mid is None:
            return
        self._con.execute("DELETE FROM transcript WHERE media_id = ?", (mid,))
        self._con.execute("DELETE FROM transcript_meta WHERE media_id = ?", (mid,))
        self._con.executemany(
            "INSERT INTO transcript (media_id, start_num, start_den, end_num, end_den, text, speaker) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (mid, *to_pair(w["start"]), *to_pair(w["end"]), w["word"], w.get("speaker"))
                for w in data.get("words", [])
            ],
        )
        self._con.execute(
            "INSERT INTO transcript_meta (media_id, language, duration_num, duration_den, "
            "text, segments, events) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mid, data.get("language"), *to_pair(data.get("duration", 0.0)),
                data.get("text", ""), json.dumps(data.get("segments", [])),
                json.dumps(data.get("events", [])),
            ),
        )
        self._con.commit()

    # -- housekeeping -------------------------------------------------------------

    def stats(self) -> dict:
        counts = {
            table: self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("media", "transcript", "analysis", "shot")
        }
        oldest = self._con.execute("SELECT min(indexed_at) FROM media").fetchone()[0]
        counts["oldest_age"] = (time.time() - oldest) if oldest is not None else None
        return counts

    def clear(self) -> None:
        self._con.execute("DELETE FROM media")
        self._con.commit()
