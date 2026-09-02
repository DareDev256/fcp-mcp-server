import pytest

from fcpxml import diversity as dv

A = {"source": "/a.mov", "caption": "man walks on beach at sunset"}
B = {"source": "/b.mov", "caption": "close up of coffee cup on table"}
C = {"source": "/c.mov", "caption": "beach sunset wide shot man walking"}


def test_passthrough_by_default():
    assert dv.apply([A, A, A]) == [A, A, A]


def test_min_separation_drops_reuse_within_window():
    assert dv.apply([A, A, B, A], min_separation=1) == [A, B, A]
    assert dv.apply([A, B, A, C], min_separation=2) == [A, B, C]


def test_ceiling_drops_near_duplicate_captions():
    assert dv.similarity(A["caption"], C["caption"]) > dv.similarity(A["caption"], B["caption"])
    assert dv.apply([A, C, B], ceiling=0.3) == [A, B]
    d = {"source": "/d.mov"}
    assert dv.apply([A, d, C], ceiling=0.3) == [A, d, C]  # no caption: never judged


def test_score():
    assert dv.score([]) == 1.0 and dv.score([A]) == 1.0
    assert dv.score([A, B, C]) == pytest.approx(1.0)
    assert dv.score([A, A, B]) == pytest.approx(0.5)
    assert "Diversity: 0.50" in dv.describe(0.5)
    assert dv.describe(0.5, [A, A, B]) == "Diversity: 0.50 (1 of 2 cuts change source)"


def test_diversity_mutation():
    """Delete apply()'s separation check and this goes red."""
    assert dv.apply([A, A], min_separation=1) == [A]
