"""NLE export, effects, audio, compound clips, templates and media relink.

Moved out of server.py, which held every handler in one 4,988-line file. The
handlers are unchanged: server.py re-exports them under their original names,
so `TOOL_HANDLERS`, the flat tool list and every test that reaches for
`server.handle_relink_media` keep working against one definition rather than
two that can disagree.

Server-owned names are reached through the bound module (`srv.X`) rather than
imported. That is not stylistic: tests monkeypatch these on the server module,
and an import here would bind a copy at import time that no patch could reach —
a guard test would keep passing while guarding nothing.
"""

from typing import Sequence

from mcp.types import TextContent

from fcpxml.export import DaVinciExporter
from fcpxml.templates import ClipSpec, apply_template, list_templates
from fcpxml.writer import list_effects
from tools import _common


async def handle_export_resolve_xml(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path = srv._resolve_io_paths(arguments, "_resolve")
    exporter = DaVinciExporter(filepath)
    exporter.export_simplified_fcpxml(
        output_path,
        flatten_compounds=arguments.get("flatten_compounds", True),
    )
    return srv._text_result((
        f"# Exported for DaVinci Resolve\n\n"
        f"- **Format**: Simplified FCPXML v1.9\n"
        f"- **Compound clips flattened**: {arguments.get('flatten_compounds', True)}\n\n"
        f"Saved to: `{output_path}`\n\n"
        f"**Next step**: In DaVinci Resolve, go to File > Import > Timeline > Import AAF/EDL/XML"
    ))


async def handle_export_fcp7_xml(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path = srv._resolve_io_paths(arguments, "_fcp7")
    exporter = DaVinciExporter(filepath)
    exporter.export_xmeml(output_path)
    return srv._text_result((
        f"# Exported as FCP7 XML (XMEML)\n\n"
        f"- **Format**: XMEML v5\n"
        f"- **Compatible with**: Premiere Pro, DaVinci Resolve, Avid Media Composer\n\n"
        f"Saved to: `{output_path}`\n\n"
        f"**Next step**: Import via File > Import in your target NLE"
    ))


# ----- v0.6.0 HANDLERS -----

async def handle_list_effects(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    effects = list_effects()
    lines = ["# Available FCP Transition Effects\n"]
    for eff in effects:
        lines.append(f"- **{eff['slug']}**: {eff['name']} (`{eff['uuid']}`)")
    return srv._text_result("\n".join(lines))


async def handle_add_audio(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path, modifier = srv._setup_modifier(arguments, "_audio")

    parent_clip_id = arguments.get("parent_clip_id")
    if parent_clip_id:
        modifier.add_audio_clip(
            parent_clip_id=parent_clip_id,
            asset_id=arguments.get("asset_id"),
            offset=arguments.get("offset", "0s"),
            duration=arguments.get("duration"),
            role=arguments.get("role", "dialogue"),
            lane=arguments.get("lane", -1),
            src=arguments.get("src"),
        )
        action = f"Added audio clip to '{parent_clip_id}'"
    else:
        modifier.add_music_bed(
            asset_id=arguments.get("asset_id"),
            duration=arguments.get("duration"),
            role=arguments.get("role", "music"),
            src=arguments.get("src"),
        )
        action = "Added music bed spanning full timeline"

    modifier.save(output_path)
    return srv._text_result(f"{action}\nSaved to: `{output_path}`")


async def handle_create_compound_clip(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path, modifier = srv._setup_modifier(arguments, "_compound")
    clip_ids = arguments["clip_ids"]
    name = arguments.get("name", "Compound Clip")
    modifier.create_compound_clip(clip_ids, name)
    modifier.save(output_path)
    return srv._text_result((
        f"Created compound clip '{name}' from {len(clip_ids)} clips.\n"
        f"Saved to: `{output_path}`"
    ))


async def handle_flatten_compound_clip(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path, modifier = srv._setup_modifier(arguments, "_flattened")
    ref_clip_id = arguments["ref_clip_id"]
    extracted = modifier.flatten_compound_clip(ref_clip_id)
    modifier.save(output_path)
    return srv._text_result((
        f"Flattened compound clip '{ref_clip_id}' into {len(extracted)} clips.\n"
        f"Saved to: `{output_path}`"
    ))


async def handle_list_templates(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    templates = list_templates()
    lines = ["# Available Timeline Templates\n"]
    for tmpl in templates:
        lines.append(f"## {tmpl['name']}")
        lines.append(f"{tmpl['description']}\n")
        lines.append("| Slot | Type | Default Duration | Lane | Required |")
        lines.append("|------|------|-----------------|------|----------|")
        for s in tmpl['slots']:
            lines.append(
                f"| {s['name']} | {s['slot_type']} | {s['default_duration']}s "
                f"| {s['lane']} | {'Yes' if s['required'] else 'No'} |"
            )
        lines.append("")
    return srv._text_result("\n".join(lines))


async def handle_apply_template(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    template_name = arguments["template_name"]
    clips_raw = arguments["clips"]
    output_path = srv._validate_output_path(arguments["output_path"], anchor_dir=srv.PROJECTS_DIR)
    fps = arguments.get("fps", 24)

    # Convert raw clips dict to ClipSpec objects
    clips_map = {}
    for slot_name, spec_data in clips_raw.items():
        if isinstance(spec_data, dict):
            clips_map[slot_name] = ClipSpec(
                asset_id=spec_data.get("asset_id"),
                src=spec_data.get("src"),
                name=spec_data.get("name", slot_name),
                duration=spec_data.get("duration"),
            )

    result_path = apply_template(template_name, clips_map, output_path, fps)
    return srv._text_result((
        f"Applied template '{template_name}' with {len(clips_map)} clips.\n"
        f"Saved to: `{result_path}`"
    ))


async def handle_relink_media(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    dry_run = arguments.get("dry_run", False)
    if dry_run:
        filepath = srv._validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))
        modifier = srv.FCPXMLModifier(filepath)
        result = modifier.relink_media(
            arguments["find"], arguments["replace"], dry_run=True
        )
        footer = "Dry run — no file written."
    else:
        filepath, output_path, modifier = srv._setup_modifier(arguments, "_relinked")
        result = modifier.relink_media(arguments["find"], arguments["replace"])
        saved = modifier.save(output_path)
        footer = f"Saved to: {saved}"

    if not result["relinked"]:
        return srv._text_result(
            f"No media paths matched prefix '{arguments['find']}' "
            f"({result['total_assets']} assets scanned). Nothing to relink."
        )

    lines = [
        f"{'Would relink' if dry_run else 'Relinked'} "
        f"{result['relinked']} media reference(s) "
        f"across {result['total_assets']} asset(s):",
        "",
    ]
    missing = 0
    for change in result["changes"]:
        mark = "✓" if change["target_exists"] else "⚠ target missing"
        if not change["target_exists"]:
            missing += 1
        lines.append(f"  {change['asset']}: {change['new']}  [{mark}]")
    if missing:
        lines.append("")
        lines.append(
            f"⚠ {missing} new path(s) do not exist on this machine — "
            f"FCP will show those clips as missing until the media is present."
        )
    lines.append("")
    lines.append(footer)
    return srv._text_result("\n".join(lines))


