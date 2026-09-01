"""Bridge detection.

SpliceKit revived under LateNite and owns the in-process power axis. We detect
it and say so. We do NOT call it — its RPC signatures have not been verified
against a live install, and writing an unverified call would be inventing an
API rather than integrating with one.
"""

import socket
import threading

import pytest

from fcpxml import bridges


@pytest.fixture(autouse=True)
def clear_cache():
    bridges._CACHE.clear()
    yield
    bridges._CACHE.clear()


def test_probe_returns_false_when_nothing_is_listening():
    assert bridges.probe(port=59999, timeout=0.1) is False


def test_probe_returns_true_when_something_is_listening():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=listener.accept, daemon=True).start()
    try:
        assert bridges.probe(port=port, timeout=2.0) is True
    finally:
        listener.close()


def test_probe_refuses_a_non_loopback_host():
    with pytest.raises(ValueError, match="loopback-only"):
        bridges.probe(port=9876, host="192.168.1.10")


def test_detect_reports_every_known_bridge():
    found = bridges.detect(refresh=True)
    assert set(found) == set(bridges.BRIDGES)
    assert all(isinstance(value, bool) for value in found.values())


def test_detect_is_cached_within_a_session(monkeypatch):
    calls = []
    monkeypatch.setattr(bridges, "probe", lambda **kw: bool(calls.append(kw)) and False)
    bridges.detect(refresh=True)
    first = len(calls)
    assert first == len(bridges.BRIDGES)
    bridges.detect()
    assert len(calls) == first, "second detect() must not re-probe"


def test_refresh_forces_a_re_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(bridges, "probe", lambda **kw: bool(calls.append(kw)) and False)
    bridges.detect(refresh=True)
    bridges.detect(refresh=True)
    assert len(calls) == 2 * len(bridges.BRIDGES)


def test_probe_never_leaves_the_loopback_interface(monkeypatch):
    seen = []
    real = socket.create_connection

    def spy(address, timeout=None):
        seen.append(address[0])
        return real(address, timeout=timeout)

    monkeypatch.setattr(socket, "create_connection", spy)
    bridges.detect(refresh=True)
    assert seen and all(host == "127.0.0.1" for host in seen)


def test_describe_names_the_manual_path_when_nothing_is_listening(monkeypatch):
    monkeypatch.setattr(bridges, "probe", lambda **kw: False)
    text = bridges.describe()
    assert "Cmd-E" in text
    assert "No control bridge" in text


def test_describe_states_that_we_do_not_call_a_detected_bridge(monkeypatch):
    """A detected bridge must not read as an automated one."""
    monkeypatch.setattr(bridges, "probe", lambda port, **kw: port == 9876)
    text = bridges.describe()
    assert "splicekit" in text
    assert "not implemented" in text
    assert "Cmd-E" in text
