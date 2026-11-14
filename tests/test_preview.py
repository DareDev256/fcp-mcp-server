"""The preview:// resource: the app shell with the timeline inlined (0.32).

Until 0.31 ``preview://`` served a separate static renderer (fcpxml/preview.py,
amber and pink blocks, then a CSS-only re-skin). 0.32 retires it: the
resource is the same ``ui://fcp/timeline`` shell the view tool's hosts draw,
with the view payload inlined as JSON, so there is one timeline picture, not
two. These read the page as served (through the real resources/read handler,
on either SDK) and draw it in Chromium.
"""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from mcp.types import ReadResourceRequest, ReadResourceRequestParams

import server as server_module
from fcpxml import timeline_app
from fcpxml.mcp_compat import is_legacy_api, resource_mime_type
from fcpxml.parser import FCPXMLParser

INLINE = re.compile(r'<script type="application/json" id="fcp-inline-payload">(.*?)</script>', re.S)


def _timeline(name="sample.fcpxml"):
    return FCPXMLParser().parse_file(f"examples/{name}").primary_timeline


def inlined(html: str) -> dict:
    m = INLINE.search(html)
    assert m, "no inlined payload"
    return json.loads(m.group(1))


class TestThePageIsTheAppWithThePayloadInlined:
    def test_the_page_is_the_shell_plus_one_json_block(self):
        tl = _timeline()
        payload = timeline_app.build_payload(tl, filepath="examples/sample.fcpxml")
        html = timeline_app.render_page_html(payload)
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.replace(INLINE.search(html).group(0) + "\n", "") == timeline_app.render_app_html()
        assert inlined(html) == json.loads(json.dumps(payload))

    def test_every_clip_is_in_the_page(self):
        tl = _timeline("music-video.fcpxml")
        payload = inlined(timeline_app.render_page_html(timeline_app.build_payload(tl)))
        assert len(payload["clips"]) == len(tl.clips) + len(tl.connected_clips)

    def test_markup_in_a_name_stays_data(self):
        """A clip named ``</script><script>alert(1)</script>`` must not end the
        JSON block: ``<`` is escaped inside it."""
        tl = _timeline()
        payload = timeline_app.build_payload(tl)
        evil = "</script><script>alert(1)</script>"
        payload["name"] = evil
        payload["clips"][0]["name"] = evil
        html = timeline_app.render_page_html(payload)
        block = INLINE.search(html).group(1)
        assert "</script>" not in block and "<script>" not in block
        assert inlined(html)["clips"][0]["name"] == evil

    def test_the_legacy_renderer_is_gone(self):
        assert not Path("fcpxml/preview.py").exists()
        assert "render_timeline_html" not in Path("server.py").read_text(encoding="utf-8")


def _read_resource(uri: str):
    """Drive the real registered resources/read handler for `uri`.

    Goes through the SDK's handler registry rather than calling
    `server_module.read_resource` directly, because the thing under test is
    partly what the SDK does to the return value — a bare str gets stamped
    text/plain no matter what mimeType list_resources advertised.

    Both SDK generations are driven, since the registry moved: 1.x keys by
    request *type* and hands the handler a whole Request; 2.x keys by method
    string and hands it `(ctx, params)`. Testing only the installed one would
    let the other rot exactly the way the decorator API did.
    """
    if is_legacy_api():
        # `method=` is passed explicitly on purpose. mcp only gained a default
        # for it after 1.3.0, so omitting it makes these tests fail with a
        # pydantic ValidationError on the oldest supported SDK while the
        # product itself works fine — exactly the kind of blind spot that let
        # the dependency floor drift.
        handler = server_module.server.request_handlers[ReadResourceRequest]
        request = ReadResourceRequest(
            method="resources/read",
            params=ReadResourceRequestParams(uri=uri),
        )
        return asyncio.run(handler(request)).root.contents

    entry = server_module.server._request_handlers["resources/read"]
    handler = getattr(entry, "handler", entry)
    params = ReadResourceRequestParams(uri=uri)
    return asyncio.run(handler(None, params)).contents


class TestPreviewResourceServesHTML:
    """Drives the real ReadResourceRequest handler, not just render_page_html directly.

    A resource that advertises mimeType="text/html" in list_resources but
    returns a bare str from read_resource gets stamped text/plain by mcp's
    lowlevel wrapper (mcp/server/lowlevel/server.py) — a client would render
    the HTML source as a wall of text instead of a document.
    """

    def test_preview_uri_is_served_with_html_mime_type(self):
        contents = _read_resource("preview://examples/sample.fcpxml")
        assert len(contents) == 1
        assert resource_mime_type(contents[0]) == "text/html"
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")
        assert inlined(contents[0].text)["name"] == _timeline().name

    def test_file_uri_is_unaffected(self):
        """The pre-existing file:// branch keeps its original text/plain behaviour."""
        contents = _read_resource("file://examples/sample.fcpxml")
        assert len(contents) == 1
        assert "FCPXML Project" in contents[0].text


class TestResourceUriWithSpacesInFilename:
    """Filenames with spaces are the norm in ~/Movies.

    `list_resources` hands the client a pydantic-normalized URI, so
    `My Project.fcpxml` comes back percent-encoded. If read_resource does not
    unquote, every one of those reads fails with "File not found" — which
    would make the flagship preview:// feature look broken for a large share
    of real libraries.
    """

    @staticmethod
    def _spaced_copy(tmp_path):
        src = Path("examples/sample.fcpxml").read_bytes()
        target = tmp_path / "My Project Final.fcpxml"
        target.write_bytes(src)
        return target

    def test_preview_uri_percent_encoded_space_resolves(self, tmp_path):
        target = self._spaced_copy(tmp_path)
        uri = "preview://" + quote(str(target))
        contents = _read_resource(uri)
        assert resource_mime_type(contents[0]) == "text/html"
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")

    def test_file_uri_percent_encoded_space_resolves(self, tmp_path):
        target = self._spaced_copy(tmp_path)
        uri = "file://" + quote(str(target))
        contents = _read_resource(uri)
        assert "FCPXML Project" in contents[0].text
        assert "File not found" not in contents[0].text

    def test_list_resources_uri_round_trips_through_read(self, tmp_path, monkeypatch):
        """End to end: whatever list_resources advertises must be readable."""
        self._spaced_copy(tmp_path)
        monkeypatch.setattr(server_module, "PROJECTS_DIR", str(tmp_path))
        resources = asyncio.run(server_module.list_resources())
        previews = [r for r in resources if str(r.uri).startswith("preview://")]
        assert previews, "expected a preview:// resource for the spaced file"
        contents = _read_resource(str(previews[0].uri))
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")

    def test_list_resources_uri_with_literal_percent_round_trips_through_read(
        self, tmp_path, monkeypatch
    ):
        """A filename containing a literal '%' must survive list_resources -> read_resource.

        `list_resources` used to interpolate the raw filesystem path straight into
        `file://{f}` / `preview://{f}` with no percent-encoding, while `read_resource`
        unconditionally `unquote()`s whatever URI it is handed. A filename like
        `pct%20lit.fcpxml` would then be emitted raw and decoded on read as if `%20`
        were an encoded space, resolving to the wrong (nonexistent) path and failing
        with "File not found" — safe, but the resource was unreachable. `quote()`
        on the way out fixes the round trip.
        """
        src = Path("examples/sample.fcpxml").read_bytes()
        target = tmp_path / "pct%20lit.fcpxml"
        target.write_bytes(src)
        monkeypatch.setattr(server_module, "PROJECTS_DIR", str(tmp_path))
        resources = asyncio.run(server_module.list_resources())
        previews = [r for r in resources if str(r.uri).startswith("preview://")]
        assert previews, "expected a preview:// resource for the percent-named file"
        contents = _read_resource(str(previews[0].uri))
        assert resource_mime_type(contents[0]) == "text/html"
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")
        assert "File not found" not in contents[0].text
