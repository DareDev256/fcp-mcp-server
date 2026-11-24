---
name: rough-cut
description: Turn raw footage or a Final Cut library export into a first assembly open in Final Cut Pro. Use when the user says "cut this footage", "make a rough cut", "assemble the best takes", "first pass of this shoot", or "build a selects reel".
---

# Rough cut

Done means: a new `.fcpxml` assembly exists, you have looked at it (view_timeline, preview_render), and push_to_fcp reported the import as observed in Final Cut. Say which file you pushed and its sha256. Do not report "done" from a file existing.

Every call is `<tool>` with `{"action": "...", "args": {...}}`. Every write answers with a `sha256` footer; pass it back as `expected_sha256` on the next call so you never edit a file the user changed in Final Cut.

1. Get a project file. Footage must be in an FCPXML (File > Export XML in Final Cut on the imported event) or authored from an edl.json:
   `generate` `{"action": "import_edl_json", "args": {"filepath": "~/Movies/shoot/edl.json", "fps": 24, "name": "Shoot"}}`
   For one extra media file on an existing project use `edit` `insert_clip` with `src`.
2. Warm the cache: `index` `{"action": "index_build", "args": {"filepath": "~/Movies/shoot/events.fcpxml", "with_transcript": false}}`. It is a cache; skipping it only makes later calls slower. It runs as a background task on clients that support tasks.
3. Find material. Dialogue: `transcript` `{"action": "transcribe_media", "args": {"filepath": "...", "backend": "local"}}`, then `{"action": "transcript_pack", "args": {"filepath": "...", "write": true}}` to read every transcript on one page. Visual or topical: `find` `{"action": "find_shots", "args": {"filepath": "...", "query": "sunset over water", "limit": 10}}`. find_shots never transcribes; it names clips with no transcript.
4. Assemble, pick one:
   - Target length with pacing: `generate` `{"action": "auto_rough_cut", "args": {"filepath": "...", "output_path": "~/Movies/shoot/rough.fcpxml", "target_duration": "3m", "pacing": "medium", "min_source_separation": 1}}`
   - Dialogue: `transcript` `edit_by_transcript` with `phrases` and `mode` "keep_only" (see the transcript-edit skill).
   - A search result: `find` `{"action": "find_to_timeline", "args": {"filepath": "...", "query": "...", "output_path": "...", "min_source_separation": 1}}`
5. Look at it before anything else: `view` `{"action": "view_timeline", "args": {"filepath": "~/Movies/shoot/rough.fcpxml"}}`, then `preview` `{"action": "preview_render", "args": {"filepath": "...", "burn_in": true}}`. Read the preview_render reply: it states anything UNVERIFIED or substituted. A render matching the file's current hash is also what unlocks deliver actions later.
6. Optional cleanup: `diagnose` `validate_timeline` (see qc-and-deliver).
7. Open it in Final Cut with the push-to-final-cut skill: `deliver` `{"action": "push_to_fcp", "args": {"filepath": "...", "library_location": "~/Movies/Shoot.fcpbundle"}}`.

Notes: auto_rough_cut reports a diversity score; if the same source repeats, raise `min_source_separation`. `output_path` is required for auto_rough_cut and optional elsewhere (default adds a suffix; originals are never overwritten). If FCP_MCP_AUTOPUSH is on, every write already pushes to Final Cut; do not push the same file again by hand without checking.

Stop and tell the user when:
- A call is refused with "changed since you last read it" (hash mismatch): re-read, then ask.
- find_shots reports no transcript or missing media, or media is not found on this machine (relink with deliver `relink_media`).
- preview_render reports UNVERIFIED or falls back to hard cuts or no lanes.
- push_to_fcp says NOT VERIFIED or refuses (see push-to-final-cut).
- If a dialog asks the user to approve something (client elicitation), wait for their answer; never pass `confirm_unreviewed=true` to get around it.
