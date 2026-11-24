---
name: generative-fill
description: Fill a gap, add AI b-roll, or extend a clip with generated video. Use when the user says "generate b-roll", "fill this gap with AI footage", "extend this clip", "what would generated footage cost", or "put something over this section".
---

# Generative fill

Done means: the user saw a price and approved it, the generation ran, the new file has a `generated` lane clip with a marker, DTD status was reported, and you previewed the result. This spends real money. Spending is the user's decision. An agent must never confirm a quote on its own, never pass `confirm: true` without the user having said yes to that quote in this conversation, and never raise the per-call cap to make a job fit.

Every call is `<tool>` with `{"action": "...", "args": {...}}`.

1. Quote: `gen` `{"action": "gen_quote", "args": {"provider": "fal", "seconds": 7, "prompt": "a street at dusk, handheld"}}`. The reply gives a quote id, billable seconds, the estimated USD with its price date, whether the price was read on the vendor page or is UNVERIFIED, and the cap (FCP_MCP_GEN_MAX_USD, default 5). `model` is optional. Runway and Luma are stubs: the quote is an estimate only.
2. Ask the user. Show provider, model, seconds, prompt, price, cap. Wait for an explicit yes. Quotes expire after 60 minutes.
3. Generate, with the SAME provider, model, seconds and prompt as the quote (the price is recomputed from the job, so a changed prompt invalidates the quote id):
   - Gap: `{"action": "gen_fill_gap", "args": {"filepath": "~/Movies/cut/final.fcpxml", "gap_index": 0, "prompt": "...", "seconds": 7, "quote_id": "<id>", "confirm": true}}` (or `at` in seconds to pick the gap)
   - B-roll over a clip: `{"action": "gen_broll", "args": {"filepath": "...", "clip_name": "Interview A", "at": 4, "seconds": 5, "prompt": "...", "quote_id": "<id>", "confirm": true, "lane": 1}}`
   - Extend past the out point: `{"action": "gen_extend_clip", "args": {"filepath": "...", "clip_name": "Interview A", "seconds": 3, "prompt": "...", "quote_id": "<id>", "confirm": true}}`
   `condition_on_frame` defaults true: the job is conditioned on a frame of the source when available, and the reply says if it fell back to the prompt alone. The generators run as background tasks where supported.
   Where the server supports elicitation and you call a generator without a confirmed quote, it asks the user to approve the spend in a dialog instead of refusing. Let them answer it. Do not work around it.
4. Read the reply: media saved under `generated/` beside the project, probed length and fps, lane, role `generated`, a marker naming prompt and provider, the new file path, sha256, estimated cost, and a DTD line. "DTD: INVALID" means do not import the file.
5. Preview: `view` `view_timeline`, then `preview` `preview_render` on the new file. Report spend: the estimate is not invoice-checked, say so.

Stop and tell the user when:
- The call refuses: no confirm, unknown or expired quote, job changed since the quote, price over the cap, or no key (FAL_KEY or GEMINI_API_KEY in the environment; never ask for the key in chat).
- The provider is video-to-video or a stub that is not implemented.
- The generated media probes as not video, or DTD is INVALID.
- The user asks to "just run it": still show the price first.
