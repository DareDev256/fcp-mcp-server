---
name: transcript-edit
description: Edit dialogue and talking-head footage by what is said. Use when the user says "cut the ums", "remove filler words", "keep only the parts where I talk about X", "tighten this interview", "cut the dead air", or "edit by transcript".
---

# Transcript edit

Done means: a new `.fcpxml` with the cuts applied, the reply's cut list matches what the user asked for, view_timeline or preview_render shows the result, and you reported each removed span and the new file path with its sha256.

Every call is `<tool>` with `{"action": "...", "args": {...}}`. Pass the last `sha256` as `expected_sha256`. Edits write a new file; the source is never overwritten.

1. Transcribe: `transcript` `{"action": "transcribe_media", "args": {"filepath": "~/Movies/interview/cut.fcpxml", "backend": "local", "model": "base", "language": "en"}}`
   - `local` runs faster-whisper on this machine; audio never leaves it.
   - `elevenlabs` uploads the audio to ElevenLabs Scribe and adds speaker labels and audio events. It needs ELEVENLABS_API_KEY in the environment and is opt-in; tell the user the audio leaves the machine before using it. A local transcript never satisfies a request for speakers.
   - Results are cached in the index, so a second call is fast. Runs as a background task where supported.
2. Read it: `{"action": "transcript_pack", "args": {"filepath": "...", "gap": 0.5, "write": true}}`. Every transcript on one page; `write` saves the full pack beside the project when the chat copy is truncated.
3. Pick the operation:
   - Fillers: `{"action": "remove_filler_words", "args": {"filepath": "...", "padding": 0.02, "output_path": "~/Movies/interview/cut_clean.fcpxml"}}` (add `fillers` to override the list)
   - By phrase: `{"action": "edit_by_transcript", "args": {"filepath": "...", "phrases": ["so basically"], "mode": "remove", "padding": 0.05}}`, or `mode` "keep_only" to keep only the matches.
   - Dead air from the real audio: `edit` `{"action": "detect_media_silence", "args": {"filepath": "...", "noise_db": -30, "min_silence": 0.5}}`, then `{"action": "remove_media_silence", "args": {"filepath": "...", "noise_db": -30, "min_silence": 0.5, "padding": 0.05}}`.
   - Gaps and clips named like silence in the timeline itself: `diagnose` `detect_silence_candidates`, then `edit` `{"action": "remove_silence_candidates", "args": {"filepath": "...", "mode": "mark"}}` first (adds markers), `delete` only after the user agrees.
4. Review: `preview` `{"action": "preview_check", "args": {"filepath": "...", "source": "~/Movies/interview/a.mov", "start": 10, "end": 15, "cut": 12.5}}` shows frames either side of a cut with its waveform; or `view` `view_timeline`. Listen-level claims need a render: `preview_render`.
5. Chain edits on the NEW file each time; always pass the new path.

Stop and tell the user when:
- The reply says no media, no transcript, or media not found on this machine.
- The backend is elevenlabs and ELEVENLABS_API_KEY is missing; do not switch to elevenlabs silently, and do not ask for the key in chat.
- A phrase matches nothing or far more than expected: show the matches and ask.
- A hash mismatch refusal appears.
- The user asked for speaker labels and the transcript is local-only.
