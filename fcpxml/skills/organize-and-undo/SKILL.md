---
name: organize-and-undo
description: Bulk-organise a Final Cut library (keywords, ratings, roles) and recover from a bad edit. Use when the user says "tag all the interview clips", "favorite these", "set roles", "organize this library", "what did you change", "undo that", or "put it back".
---

# Organize and undo

Done means: the selection matched the clips the user meant (the reply states the count and the filter), the new file was written, and for an undo, the reply lists what moved and where. Report the output path and sha256, not "done".

Every call is `<tool>` with `{"action": "...", "args": {...}}`. Every action that takes a `filepath` honours `expected_sha256`: pass the sha256 from your last result and the call refuses if the file on disk no longer hashes to it (the user saved over it in Final Cut, another session wrote it, the path points at a different project). It applies to reads too. Without it nothing is checked, so pass it on every write after the first.

1. Look first: `inspect` `{"action": "analyze_timeline", "args": {"filepath": "~/Movies/library/event.fcpxml"}}` (it reports the current hash), `list_keywords`, `list_roles`.
2. Organize. Selection is any of `clip_name`, `keyword`, `role`; none selects every clip. Zero matches writes nothing.
   - Keywords: `organize` `{"action": "organize_keywords", "args": {"filepath": "...", "keywords": ["interview", "A-cam"], "mode": "add", "role": "dialogue", "output_path": "~/Movies/library/event_organized.fcpxml"}}`
   - Ratings: `{"action": "organize_rate", "args": {"filepath": "...", "rating": "favorite", "keyword": "hero"}}` (rating is favorite, rejected or clear)
   - Roles: `{"action": "organize_roles", "args": {"filepath": "...", "audio_role": "dialogue", "video_role": "video", "clip_name": "Interview A"}}`
   - Suggested keywords from the media: `{"action": "organize_auto", "args": {"filepath": "...", "max_keywords": 3, "apply": false}}`. It derives keywords only from captions or transcripts already in the index (run find_index and transcribe_media first). It proposes only; set `apply` true after the user approves the list.
3. Review what was written: `organize` `{"action": "history", "args": {"filepath": "...", "limit": 20}}` lists every journaled output, newest first, each with its hash. The journal lives under `~/.fcp-mcp/journal` (one ledger per input folder; FCP_MCP_JOURNAL relocates it, `off` disables it).
4. Undo: `{"action": "undo", "args": {"filepath": "...", "n": 1}}`. It moves the outputs of the last n write operations to `undone/` in the journal directory. It never deletes. It refuses and names the file when an output's hash differs from what was recorded, because the user edited it since; do not work around that by hand. Undo moves outputs; it does not change the original input file.
5. Confirm: run `history` again and `ls` the project folder to show the output gone from where it was and present under undone/.

Stop and tell the user when:
- A refusal says the file changed since you last read it (expected_sha256 mismatch), or an undo refuses on hash mismatch.
- The reply says the journal is off, so nothing was recorded and nothing can be undone.
- The selection matched zero clips or far more than the user described.
- The user wants an undo of something that happened outside the journal (an edit made in Final Cut itself).
