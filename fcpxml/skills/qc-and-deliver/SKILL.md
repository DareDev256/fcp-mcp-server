---
name: qc-and-deliver
description: Quality-check a Final Cut timeline, fix what is broken, then export it. Use when the user says "check this timeline", "any gaps or flash frames", "export for Resolve", "give me an EDL", "send it to the colorist", "export stems", or "deliver it".
---

# QC and deliver

Done means: validate_timeline is clean (or each remaining finding is named to the user), the final file was rendered with preview_render, the export ran, and you confirmed the output file exists with its size. Do not say "exported" from a tool reply alone.

Every call is `<tool>` with `{"action": "...", "args": {...}}`. Pass the latest `sha256` as `expected_sha256` so a file the user saved over in Final Cut is refused rather than overwritten.

1. Validate: `diagnose` `{"action": "validate_timeline", "args": {"filepath": "~/Movies/cut/final.fcpxml"}}`. It checks structure, transition handles, media clock and flash frames in one table.
2. Drill into what it flags, as needed (all read-only):
   - `{"action": "detect_gaps", "args": {"filepath": "...", "min_gap_frames": 1}}`
   - `{"action": "detect_flash_frames", "args": {"filepath": "...", "critical_threshold_frames": 2, "warning_threshold_frames": 6}}`
   - `{"action": "detect_duplicates", "args": {"filepath": "..."}}`
   - `{"action": "find_short_cuts", "args": {"filepath": "...", "threshold_seconds": 0.5}}`
3. Fix, each writes a new file and returns a new sha256:
   - `edit` `{"action": "fill_gaps", "args": {"filepath": "...", "mode": "extend_previous", "output_path": "~/Movies/cut/final_fixed.fcpxml"}}` (mode is extend_previous, extend_next or delete)
   - `edit` `{"action": "fix_flash_frames", "args": {"filepath": "...", "threshold_frames": 2, "output_path": "..."}}`
   Re-run validate_timeline on the NEW file. A fix that was not re-validated is unproven.
4. Review gate. `preview` `{"action": "preview_render", "args": {"filepath": "~/Movies/cut/final_fixed.fcpxml"}}`. The six gated deliver actions (export_csv, export_edl, export_fcp7_xml, export_resolve_xml, export_role_stems, push_to_fcp) refuse unless the journal holds a preview_render whose input sha256 equals the file as it is now. Edit the file again and the old render stops counting; render again. On clients that support elicitation the server instead asks the user in a dialog when no matching render exists. Do not pass `confirm_unreviewed=true` to skip the review or the dialog; that is the user's decision, and the export is then stamped UNREVIEWED.
5. Export what the user asked for:
   - Resolve: `deliver` `{"action": "export_resolve_xml", "args": {"filepath": "...", "flatten_compounds": true, "output_path": "~/Movies/cut/final_resolve.fcpxml"}}`
   - Premiere / FCP7: `{"action": "export_fcp7_xml", "args": {"filepath": "...", "output_path": "..."}}`
   - EDL: `{"action": "export_edl", "args": {"filepath": "..."}}`
   - Sheet: `{"action": "export_csv", "args": {"filepath": "..."}}`
   - Audio stems by role: `{"action": "export_role_stems", "args": {"filepath": "..."}}` (runs as a background task where supported).
6. Confirm the output: take the path from the reply and run `ls -l` on it. Report path and size.

Stop and tell the user when:
- Any call is refused for a hash mismatch (the file changed since you read it).
- A gate refusal names preview_render and preview_render itself fails or reports UNVERIFIED.
- validate_timeline shows FAIL rows you cannot fix (missing media, handle shortfall on a transition, media clock mismatch).
- The export file is missing or empty, or the reply says the journal is off (FCP_MCP_JOURNAL), which disables the gate's evidence.
