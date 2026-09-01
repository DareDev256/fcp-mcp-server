---
name: final-cut-pro
description: Edit Final Cut Pro timelines with natural language via FCPXML. Use when the user wants to analyze, cut, trim, mark up, transcribe, montage, or export a Final Cut Pro project, mentions .fcpxml or .fcpxmld files, asks to remove silence or filler words from a video edit, wants a rough cut assembled from source clips, wants markers or chapters added from a transcript or SRT, or wants an edit pushed into a running Final Cut Pro. Requires the fcp-mcp-server MCP server.
---

# Final Cut Pro editing via FCPXML

## Before you touch anything

Always `inspect` then `diagnose` before you `edit`. Timelines carry structure
that is not obvious from a filename, and an edit against a wrong assumption is
expensive to undo.

1. `inspect` with action `analyze_timeline` for shape, duration, resolution.
2. `diagnose` with action `validate_timeline` for gaps, flash frames, duplicates.
3. Read the `preview://<path>` resource to actually SEE the timeline before
   you change anything. It renders a self-contained HTML timeline, including
   connected clips on their own lane rows, so you can see B-roll and audio
   layers, not just the primary storyline.

## The seven groups

| Group | Use it for |
|---|---|
| `inspect` | Read-only understanding. Start here, always. |
| `diagnose` | Find problems: gaps, flash frames, dead air, duplicates, beats. |
| `edit` | Change clips: insert, delete, trim, split, reorder, retime, remove silence. |
| `mark` | Markers and chapters, including SRT/VTT and beat import. |
| `generate` | Build new structure: rough cuts, montages, A/B roll, templates. |
| `transcript` | Transcribe locally, then edit or clean up by what was SAID. |
| `deliver` | Export to other NLEs, reformat, relink, push into FCP. |
| `preview` | SEE the edit: proxy render, contact sheet, and a filmstrip+waveform read from the source media. |
| `watch` | Close the round-trip: notice the operator's Cmd-E export and diff it against the last one. |

Every call takes `{"action": "...", "args": {...}}`. If you pass an action the
group does not own, the error lists the valid ones — read it rather than
guessing.

## FCPXML facts that will bite you

- **Time is rational, never float.** `3600/2400s` is 1.5 seconds. Never do
  float math on timecode and never round it yourself.
- **`offset` is the timeline position. `start` is the source in-point.** They
  are different numbers and confusing them silently shifts an edit.
- **Library clips and timeline clips are different elements.** `<asset-clip>`
  lives in the library, `<clip>` lives on the timeline.
- **Markers are children of clips, not siblings.**
- **The `<spine>` is the primary storyline.** Connected clips hang off spine
  clips by lane: positive is above (video), negative is below (audio).
- **`.fcpxmld` bundles are directories**, wrapping `Info.fcpxml` plus sidecar
  data. Sidecars must be preserved on save or object-tracking and Cinematic
  data is destroyed.
- **Duplicate clip names are common.** Prefer element-based operations over
  name lookups where a tool offers both.

## Silence removal: pick the right one

There are two detect-then-fix pairs, and they are not interchangeable. Both
follow the same split as every other detect/fix pair in this server: the
detector lives in `diagnose`, the fixer lives in `edit`.

- `detect_media_silence` (`diagnose`) → `remove_media_silence` (`edit`) reads
  the **actual source audio** with ffmpeg. Accurate, slower, needs ffmpeg
  installed.
- `detect_silence_candidates` (`diagnose`) → `remove_silence_candidates`
  (`edit`) uses **timeline heuristics** only. Fast, no ffmpeg, and it guesses.

Use the media versions when correctness matters. Say which one you used.

## Working order that tends to hold

1. `inspect` → `diagnose` → read the `preview://` resource.
2. `transcript` with action `transcribe_media` if the edit is dialogue-driven.
3. `generate` for the assembly, `edit` for the refinement, `mark` for chapters.
4. `preview` with action `preview_check` over the range you changed, before
   offering the edit as done. This is what makes the edit non-blind. The
   `preview://` resource and `preview_timeline` both draw from the XML — they
   show what was WRITTEN, so they cannot tell a fixed cut from a broken one.
   `preview_check` reads the media. Do not substitute one for the other, and
   do not skip it because the tool call reported success.
5. `deliver`, either exporting or `push_to_fcp` into the running app.
6. `watch` with action `watch_start` once per session, then `watch_pull` after
   the operator exports. Final Cut Pro has NO programmatic export — Apple never
   shipped one — so the loop only closes when they press Cmd-E. Say so plainly
   rather than waiting in silence.

## Do not

- Do not edit without a diagnose pass. Flash frames and gaps compound.
- Do not assume ffmpeg or Whisper are installed. Both degrade gracefully and
  return nothing rather than erroring, so check the response.
- Do not read a `preview_render` summary past the word UNVERIFIED. It means the
  rendered file's duration could not be read back, so nothing about that render
  has been confirmed.
- Do not ignore a `Substituted:` line. The proxy renders transitions as hard
  cuts, so a dissolve in the timeline is NOT what you are looking at.
- Do not claim an edit landed in Final Cut Pro unless `push_to_fcp` was called
  and reported success. Writing an .fcpxml file is not the same as importing it.
- Do not trust a tool's own success message as proof the timeline looks
  right. Read `preview://` and look.
