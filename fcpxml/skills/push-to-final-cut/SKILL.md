---
name: push-to-final-cut
description: Send a timeline into the running Final Cut Pro and prove it landed. Use when the user says "open it in Final Cut", "push this to FCP", "is it in Final Cut", "import this into my library", or when the push hangs, asks for a library, or Automation is denied.
---

# Push to Final Cut

Done means: push_to_fcp replied "Imported into Final Cut Pro: <project names>" (verified), or you told the user plainly that it was sent but NOT VERIFIED. A send is never a success. osascript returning 0 only means the Apple event was delivered.

Every call is `<tool>` with `{"action": "...", "args": {...}}`.

1. See what is open: `deliver` `{"action": "list_fcp_libraries", "args": {"allow_launch": false}}`. It lists open libraries, events and projects, and will not launch Final Cut unless `allow_launch` is true.
2. Choose the target. Never guess it. If exactly one library is open it is inferred; with several open, or Final Cut closed, the call refuses and names them. On clients that support elicitation the server asks the user which library in a dialog when `library_location` is missing and more than one is open. Without a dialog, pass it yourself.
3. Push: `deliver` `{"action": "push_to_fcp", "args": {"filepath": "~/Movies/cut/final.fcpxml", "library_location": "~/Movies/Shoot.fcpbundle", "verify": true, "verify_timeout": 60}}`
   - A `.fcpbundle` path that does not exist yet is created by Final Cut, so only pass a new path when the user wants a new library.
   - `copy_assets` true copies media into the library; omit it for the default.
   - The call blocks up to `verify_timeout`. It always sends an `_import` copy with import options injected, never your original.
   - push_to_fcp is a gated deliver action: it needs a matching preview_render (see qc-and-deliver). Do not pass `confirm_unreviewed=true`; let the user decide at the dialog.
4. Read the reply. Verified imports say "Imported into Final Cut Pro". "Sent to Final Cut Pro" plus "NOT VERIFIED: <reason>" means the import was not observed (verification skipped, or a project with that name already existed so its presence proves nothing). Report that wording, then check with list_fcp_libraries and tell the user what you see.

Known failures:
- Automation error -1743 or "not allowed": the host app lacks permission to control Final Cut. The user grants it in System Settings > Privacy & Security > Automation. A pending permission prompt can sit behind other windows and holds every event until it is answered. The server falls back to Launch Services and proves the import from the library bundle on disk; say that is how it was checked.
- A picker "Which library do you want to import into?" in Final Cut 12.4, or a modal "Open Library" or Import XML sheet: the document had no library location. The root-cause fix is always passing `library_location`, not clicking through. A modal sheet already open in Final Cut also blocks Apple events until the user dismisses it.
- "was never observed ... within Ns": the import did not land. Likely a media-identity collision (same media already in that library under another id; push into a fresh library) or a modal waiting in Final Cut. Do not retry in a loop.
- If FCP_MCP_AUTOPUSH is on, every write already pushed; a manual push may duplicate.

Stop and tell the user when:
- The reply says NOT VERIFIED, or raises an import-not-observed error.
- Automation is denied or Final Cut is blocked by a dialog (the user must click).
- Several libraries are open and no dialog can ask which one.
- The review gate refuses and the user has not approved shipping without a render.
