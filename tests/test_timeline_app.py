"""The MCP Apps timeline: the resource, the tool binding and the payload.

The payload is the contract. Every position and duration goes out as an
exact rational string, and these tests read the fixtures back through it
and compare Fractions, never floats — a float at 23.976 would pass a
tolerance check while drifting a frame by the end of the reel.
"""

import asyncio
import json
from fractions import Fraction
from pathlib import Path

import pytest
from mcp.types import ReadResourceRequest, ReadResourceRequestParams

import server as server_module
import tools
from fcpxml import timeline_app
from fcpxml.mcp_compat import (
    APP_MIME_TYPE,
    APPS_EXTENSION_ID,
    advertises_extensions,
    initialization_options,
    is_legacy_api,
    resource_mime_type,
    tool_meta,
)
from fcpxml.models import Clip, ConnectedClip, Marker, Timecode, Timeline
from fcpxml.parser import FCPXMLParser

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = [str(EXAMPLES / "sample.fcpxml"), str(EXAMPLES / "music-video.fcpxml")]


def _read_resource(uri: str):
    """Drive the real registered resources/read handler on either SDK."""
    if is_legacy_api():
        handler = server_module.server.request_handlers[ReadResourceRequest]
        request = ReadResourceRequest(
            method="resources/read", params=ReadResourceRequestParams(uri=uri),
        )
        return asyncio.run(handler(request)).root.contents
    entry = server_module.server._request_handlers["resources/read"]
    handler = getattr(entry, "handler", entry)
    return asyncio.run(handler(None, ReadResourceRequestParams(uri=uri))).contents


# ---------------------------------------------------------------------------
# The resource and the binding
# ---------------------------------------------------------------------------

def test_app_resource_is_served_with_the_mcp_app_mime_type():
    contents = _read_resource(timeline_app.APP_URI)
    assert len(contents) == 1
    assert resource_mime_type(contents[0]) == "text/html;profile=mcp-app" == APP_MIME_TYPE
    assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")


def test_app_resource_is_listed_with_its_mime_type():
    resources = asyncio.run(server_module.list_resources())
    app = [r for r in resources if str(r.uri) == timeline_app.APP_URI]
    assert len(app) == 1
    assert resource_mime_type(app[0]) == APP_MIME_TYPE


def test_an_unknown_ui_uri_is_refused_by_name():
    text = asyncio.run(server_module.read_resource("ui://fcp/nope"))
    assert "Unknown app resource" in text and timeline_app.APP_URI in text


def test_view_tool_advertises_the_resource_uri_on_the_wire():
    tools_list = asyncio.run(server_module.list_tools())
    (view,) = [t for t in tools_list if t.name == "view"]
    assert tool_meta(view) == {"ui": {"resourceUri": timeline_app.APP_URI}}
    wire = json.loads(view.model_dump_json(by_alias=True, exclude_none=True))
    # `_meta` is the field name an MCP Apps host reads; on 1.x it is an
    # extra, on 2.x an alias. Either way this is what leaves the process.
    assert wire["_meta"]["ui"]["resourceUri"] == "ui://fcp/timeline"


def test_no_other_group_carries_ui_meta():
    tools_list = asyncio.run(server_module.list_tools())
    for t in tools_list:
        if t.name != "view":
            assert tool_meta(t) is None, t.name


def test_a_group_binding_a_non_ui_uri_is_refused():
    with pytest.raises(ValueError, match="ui://"):
        tools.register_group(
            "bogus", "x", {"bogus_action": None}, meta={"ui": {"resourceUri": "file:///x"}}
        )
    assert "bogus" not in tools.EXTRA_GROUPS


def test_extension_is_advertised_where_the_sdk_has_the_slot():
    options = initialization_options(server_module.server, {APPS_EXTENSION_ID: {}})
    extensions = getattr(options.capabilities, "extensions", None)
    if advertises_extensions():
        assert APPS_EXTENSION_ID in (extensions or {})
    else:
        assert extensions is None  # 1.x: no slot, and we do not invent one


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------

EXTERNAL = ("http://", "https://", "<link", "@import", "url(", 'src="//', "integrity=", "fetch(")


def test_shell_reaches_nowhere():
    html = timeline_app.render_app_html()
    for needle in EXTERNAL:
        assert needle not in html, needle
    assert "<script src" not in html


def test_shell_speaks_the_apps_protocol_and_calls_back_through_tools_call():
    html = timeline_app.render_app_html()
    for method in ("ui/initialize", "ui/notifications/initialized",
                   "ui/notifications/tool-result", "tools/call"):
        assert method in html, method
    # Every edit the app offers goes through an existing group, never a
    # write path of its own.
    assert '"edit"' in html or "edit(" in html
    assert "delete_clips" in html and "add_marker" in html
    assert "expected_sha256" in html


def test_shell_is_dark_with_no_cream_ground():
    html = timeline_app.render_app_html()
    assert "--bg: #09090b" in html
    for banned in ("#F7F1E7", "#FAF6EF", "#F8F2E5", "#C8862B", "#fdfbf7", "beige", "cream"):
        assert banned.lower() not in html.lower(), banned


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

def _origin(tl) -> Fraction:
    starts = [Fraction(c.start._exact_seconds) for c in tl.clips]
    starts += [Fraction(c.offset._exact_seconds) for c in tl.connected_clips
               if c.offset and c.timeline_start is None]
    return min(starts) if starts else Fraction(0)


@pytest.mark.parametrize("path", FIXTURES)
def test_payload_round_trips_the_fixture_exactly(path):
    tl = FCPXMLParser().parse_file(path).primary_timeline
    payload = timeline_app.build_payload(tl, filepath=path)
    origin = _origin(tl)

    assert Fraction(payload["duration"]) == Fraction(tl.duration._exact_seconds)
    assert Fraction(payload["origin"]) == origin
    assert len(payload["clips"]) == len(tl.clips) + len(tl.connected_clips)

    spine = [c for c in payload["clips"] if c["kind"] == "spine"]
    for row, clip in zip(spine, tl.clips):
        assert row["name"] == clip.name
        assert Fraction(row["start"]) == Fraction(clip.start._exact_seconds) - origin
        assert Fraction(row["duration"]) == Fraction(clip.duration._exact_seconds)
        assert Fraction(row["end"]) == Fraction(row["start"]) + Fraction(row["duration"])
        if clip.source_start is not None:
            assert Fraction(row["source_start"]) == Fraction(clip.source_start._exact_seconds)

    connected = [c for c in payload["clips"] if c["kind"] != "spine"]
    for row, cc in zip(connected, tl.connected_clips):
        assert row["name"] == cc.name and row["lane"] == cc.lane
        assert Fraction(row["start"]) == Fraction(cc.timeline_start._exact_seconds)
        assert Fraction(row["duration"]) == Fraction(cc.duration._exact_seconds)

    for row, marker in zip(payload["markers"], tl.markers):
        assert row["name"] == marker.name
        assert Fraction(row["start"]) == Fraction(marker.position._exact_seconds) - origin

    # Every time string is a bare n/d with no float anywhere in it.
    for row in payload["clips"]:
        for key in ("start", "duration", "end", "source_start"):
            n, d = row[key].split("/")
            assert n.lstrip("-").isdigit() and d.isdigit(), row[key]


@pytest.mark.parametrize("path", FIXTURES)
def test_wrap_and_extract_are_inverses_on_the_fixture(path):
    tl = FCPXMLParser().parse_file(path).primary_timeline
    payload = timeline_app.build_payload(tl, filepath=path)
    text = timeline_app.wrap_payload("summary line", payload)
    assert text.startswith("summary line")
    assert timeline_app.extract_payload(text) == payload
    assert timeline_app.extract_payload("no payload here") is None


def test_gap_based_fixture_reports_the_spine_as_one_gap():
    tl = FCPXMLParser().parse_file(FIXTURES[1]).primary_timeline
    payload = timeline_app.build_payload(tl)
    assert payload["gap_based"] is True
    assert payload["gaps"] == [{"start": "0/1", "duration": payload["duration"]}]
    assert [ln["lane"] for ln in payload["lanes"]] == [2, 1, -1]
    assert payload["lanes"][-1]["kind"] == "audio"


def test_sample_fixture_gap_is_the_uncovered_tail():
    tl = FCPXMLParser().parse_file(FIXTURES[0]).primary_timeline
    payload = timeline_app.build_payload(tl)
    last_end = max(Fraction(c["end"]) for c in payload["clips"])
    assert payload["gaps"] == [{
        "start": timeline_app.rational(last_end),
        "duration": timeline_app.rational(Fraction(payload["duration"]) - last_end),
    }]


def _tc(seconds, fps=24.0) -> Timecode:
    return Timecode(frames=int(Fraction(seconds) * 24), frame_rate=fps)


def _broadcast_timeline() -> Timeline:
    """A spine starting at 01:00:00:00 with a marker on it and a lane clip."""
    marker = Marker(name="hit", start=_tc(12), timeline_start=_tc(3605))
    spine = Clip(name="A", start=_tc(3600), duration=_tc(10), source_start=_tc(7), markers=[marker])
    lane = ConnectedClip(
        name="B", start=_tc(0), duration=_tc(4), lane=1, offset=_tc(3602),
        timeline_start=_tc(2), markers=[Marker(name="lane-hit", start=_tc(1), timeline_start=_tc(3))],
    )
    return Timeline(
        name="broadcast", duration=_tc(10), frame_rate=24.0,
        clips=[spine], connected_clips=[lane],
        markers=[Marker(name="chapter", start=_tc(3606))],
    )


def test_markers_land_on_the_sequence_clock_whichever_clock_hosted_them():
    payload = timeline_app.build_payload(_broadcast_timeline())
    assert payload["origin"] == "3600/1"
    spine, lane = payload["clips"]
    assert spine["start"] == "0/1" and lane["start"] == "2/1"
    assert spine["markers"] == [{"name": "hit", "start": "5/1", "type": "standard", "note": ""}]
    assert lane["markers"][0]["start"] == "3/1"
    assert payload["markers"][0]["start"] == "6/1"


def test_transcript_words_are_clipped_to_the_source_range_and_moved_to_the_timeline():
    tl = _broadcast_timeline()
    seen = []

    def transcript(media):
        seen.append(media)
        return {"words": [
            {"word": "before", "start": 5.0, "end": 6.0},   # ends before source in (7)
            {"word": "edge", "start": 6.5, "end": 7.5},     # straddles source in
            {"word": "inside", "start": 10.0, "end": 10.5},
            {"word": "after", "start": 17.0, "end": 18.0},  # starts after source out (17)
        ]}

    tl.clips[0].media_path = "file:///m/a.mov"
    payload = timeline_app.build_payload(tl, transcript=transcript)
    words = payload["clips"][0]["words"]
    assert [w["w"] for w in words] == ["edge", "inside"]
    assert words[0] == {"w": "edge", "s": "0/1", "e": "1/2"}
    assert words[1] == {"w": "inside", "s": "3/1", "e": "7/2"}
    assert seen == ["file:///m/a.mov"]  # once per media, never per clip


def test_thumbnails_are_bounded_by_count_and_bytes():
    clips = [
        Clip(name=f"c{i}", start=_tc(i), duration=_tc(1), media_path=f"file:///m/{i}.mov")
        for i in range(80)
    ]
    tl = Timeline(name="many", duration=_tc(80), frame_rate=24.0, clips=clips)

    calls = []
    payload = timeline_app.build_payload(tl, thumbnail=lambda m, at: (calls.append(at), "data:image/jpeg;base64,AAAA")[1])
    assert len(calls) == timeline_app.MAX_THUMBNAILS
    assert sum(1 for c in payload["clips"] if c["thumb"]) == timeline_app.MAX_THUMBNAILS
    # The sample position is mid-clip, in SOURCE seconds.
    assert calls[0] == Fraction(1, 2)

    big = "data:image/jpeg;base64," + "A" * 150_000
    calls.clear()
    timeline_app.build_payload(tl, thumbnail=lambda m, at: (calls.append(at), big)[1])
    assert len(calls) == 3  # 450 KB crosses the 400 KB cap; the fourth is never asked for


def test_a_thumbnail_that_fails_costs_nothing_from_the_budget():
    clips = [Clip(name=f"c{i}", start=_tc(i), duration=_tc(1), media_path="file:///m/x.mov") for i in range(60)]
    tl = Timeline(name="t", duration=_tc(60), frame_rate=24.0, clips=clips)
    calls = []
    timeline_app.build_payload(tl, thumbnail=lambda m, at: (calls.append(1), None)[1])
    assert len(calls) == 60


def test_rational_formats_fractions_and_timecodes_exactly():
    assert timeline_app.rational(Fraction(24024, 24000)) == "1001/1000"
    assert timeline_app.rational(_tc(Fraction(1, 24))) == "1/24"
    assert timeline_app.rational(None) is None
