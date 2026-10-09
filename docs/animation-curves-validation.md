# Animation curve validation — local 2026-10-08

This record covers native opacity easing and `edit.set_animation_curve`.
API usage and current limitations are in [keyframes.md](keyframes.md).
Tests used Final Cut Pro 12.4 with Apple FCPXML 1.14 on macOS. This record
does not claim a PyPI release or an update to any already-running MCP
client.

## Native interpolation probes

A synthetic six-second, 720p/30fps clip starts at source/local time 5s. Its
three intrinsic keys are at clip-relative 0, 2.5 and 5s. A twelve-project
matrix tried `ease`, `easeIn` and `easeOut` on position, scale, rotation and
opacity, followed by FCP import and native XML export.

- Position and scale `interp` presets produced import warnings and were
  removed by FCP. Rotation presets were silently removed.
- Opacity retained all three presets and all three key times/values. At
  relative 1s, the easeIn fixture read 62.67% in the Inspector; its linear
  reference is 52%. The API writes the native export's `interp`-only form.
- `curve="smooth"` alone imported without warnings and could show Smooth
  checked in the viewer menu, but the path remained straight between keys.
  A triangle probe at relative 1s read [57.6,57.6] pixels, the linear value.
  Switching imported points to Smooth through the GUI did not supply a
  usable XML tangent representation.
- Nested X/Y parameter probes collapsed to vector position keys with easing
  discarded; nested scale probes were ignored. No custom native Bezier
  handle format is claimed or exposed.

These results are narrower than the general DTD grammar. Supported inputs
therefore allow nonlinear native `interp` only on opacity and reject
`curve="smooth"`. Existing metadata remains inspectable/preserved by ordinary
keyframe edits unless the caller explicitly normalizes interpolation.

## Adaptive Bezier import and native round trip

A real MCP call authored the example in `keyframes.md`: endpoints [-40,0]
and [40,0] over five seconds, controls [-20,40] and [20,40], and outgoing
smoothstep easing. With tolerance 0.05 position units, the sampler checked
151 project frames and emitted **57 linear keys**, preserving the two authored
endpoints. Its maximum computed frame error was 0.0491499645 units.

FCP imported the generated file without warnings. The viewer displayed the
arch; Inspector positions in pixels were:

| Relative time | X | Y |
|---|---:|---:|
| 0s | -288.0 | 0 |
| 1.5s | -177.1 | 146.1 |
| 2.5s | 0 | 216.0 |
| 5s | 288.0 | 0 |

Playback reached 00:00:05:29 and stopped. Native FCPXML 1.14 export retained
all **57 keys and exact rational times**. The largest value-rounding difference
was 0.000062065 units. An independent polynomial evaluation of all 151 frame
times against the native export's linear interpolation found maximum error
0.049162243 units, or **0.35397 pixels at 720p**. This fixture therefore also
remained below the requested 0.05 bound after the observed FCP rounding;
other exports are not guaranteed to retain that bound.

A separate numerical example with controls [0,40]/[40,40], endpoints
[0,0]/[40,0] and linear parameter speed used 33 keys for 151 frames at the same
0.05 tolerance. Compression depends on geometry, easing and tolerance.

The authored controls are request data, not native handles stored in the
result. FCP receives an adaptive linear approximation. The error contract
covers project frame times between authored endpoints, not subframe motion,
subjective motion quality, or other FCP versions. Volume and speed/timeMap
curves are outside this new action.

## Automated checks and local evidence

Final full-suite results:

- Python 3.12.13 / MCP 1.30.0: **2181 passed, 9 skipped**, two pre-existing
  resource-return deprecation warnings.
- Python 3.13.15 / MCP 2.3.0: **2184 passed, 6 skipped**, no warnings.
- Whole-repository Ruff and diff whitespace checks passed.
- Real stdio `ClientSession` calls on both SDKs discovered all 13 groups,
  validated their schemas with Draft7, created/read back the adaptive curve
  and three-point native opacity easing, passed Apple DTD validation, and
  confirmed unchanged input hashes. Inventory: 96 handlers, 71 flat schemas,
  84 tools when legacy advertisement is enabled.


`tests/test_animation_curves.py` independently evaluates cubic polynomials,
checks all evaluated frame errors, authored-knot preservation, NTSC rational
times, nonlinear timing on a straight path, bounds and resource limits.
`tests/test_animation_curve_tools.py` exercises real handler dispatch,
flat/grouped schemas, exact clip selection, output-clock timing on retimed
clips, source/no-overwrite/journal protection, bundle sidecars and failure
atomicity. Existing native keyframe tests cover opacity presets and rejected
unsupported property/mode combinations against available Apple DTDs.

The ignored `.local-validation/curves/` directory contains the request,
MCP result, synthetic import XML, native FCP exports, and numerical report:
`adaptive-bezier-request.json`, `adaptive-bezier-result.json`,
`adaptive-bezier-result.fcpxml`, `adaptive-bezier-native.fcpxmld`,
`native-roundtrip-report.json`, and the native easing/nested-axis matrices.
Only synthetic test projects were edited. No user production project was
used as a write target.

## Key selection change

After the FCP import above, key selection gained a forward reach: from each
kept key, double the span while the chord stays within tolerance, then
bisect. It runs alongside the recursive worst-point split and the smaller
result is kept. Reach alone is not safe: chord error is not monotone in the
span, and on a 19-point oscillating rotation at tolerance 0.01 it needed
1003 keys, over the 1000 limit, where the split needs 988 (exhaustive
minimum 986). The error contract and its checks are unchanged. The `keyframes.md` request now emits **39 keys** instead of 57
(maximum computed frame error 0.0493805 units), and the separate
[0,40]/[40,40] example 30 instead of 33.

The same request on a renamed copy of the input (`forward-reach-*` in
`.local-validation/curves/`) was imported into FCP 12.4 without warnings.
Stepping the viewer's next-keyframe button stopped 38 times after frame 0
and not again after 00:00:05:00, so FCP holds all **39 keys** at the
generated frames. Inspector positions matched the generated linear keys
within the 0.1 px display precision (largest difference 0.044 px):

| Frame | Generated px | FCP Inspector px |
|---:|---|---|
| 0 | -288.000, 0.000 | -288.0, 0 |
| 1 | -287.829, 0.341 | -287.8, 0.3 |
| 42 (key) | -191.568, 133.662 | -191.6, 133.7 |
| 45 (1.5s) | -177.144, 146.111 | -177.1, 146.1 |
| 75 (2.5s, key) | 0.000, 216.000 | 0, 216.0 |
| 150 (5s, key) | 288.000, 0.000 | 288.0, 0 |

At frame 45 the exact cubic is -177.43, 146.31 px, so the Inspector shows
the linear approximation, 0.354 px from the curve, as intended.

FCP's native FCPXML 1.14 export (`Adaptive Bezier Arch 39 keys.fcpxmld`)
kept all **39 keys with exact rational times**, each `curve="linear"`.
Values were rounded to six significant digits; the largest change was
0.000062065 units, at frame 55. Evaluating the exact cubic at all 151 frames
against the export's linear interpolation gave maximum error **0.049364
units, or 0.35542 pixels at 720p**, below the 0.05 tolerance after FCP's
rounding. The comparison script, `native_report.py`, reproduces the 57-key
report above exactly when pointed at that export. Results are in `forward-reach-inspector.json`
and `forward-reach-native-report.json`.

`tests/test_animation_curves.py` compares four curves against an exhaustive
minimum and allows at most one extra key; the split alone was 5–18 keys
above it. It also keeps the oscillating rotation above under the limit with
at most 988 keys. Patching in either selection alone fails these tests. An
18,000-frame arch uses 36 keys (the split alone used 65), checked at every
frame. Regenerating the FCP-verified request after this change produced
the same 39 keys.

Full suite after the change, with pydantic 2.14.0 and Ruff and diff
whitespace checks passing:

- Python 3.12.13 / MCP 1.21.1, the declared floor: **2224 passed, 9 skipped**,
  the same two resource-return deprecation warnings; **2228 passed, 5
  skipped** with the `intelligence` extra, as the CI floor job installs it.
- Python 3.12.13 / MCP 2.3.0: **2227 passed, 6 skipped**.
- The local-socket probe in `test_bridges.py` was run on its own because the
  sandbox blocks binding; it passed.
