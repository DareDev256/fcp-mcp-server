# Keyframe extension validation — 2026-10-07

Base: upstream `2b39ac071f851561d134206d16d2f26f1a8ee91b` (source version
0.25.1), fork branch `codex/keyframe-animation`. No upstream release or PyPI
publication is implied. The separate installed 0.16.0 MCP was not replaced.

## Scope

Five intrinsic parameters: position, scale, rotation, opacity and volume.
Four actions: list, set, delete and batch. All test media is synthetic;
existing user footage and projects are excluded from fixtures and Git.

## Automated evidence

- Python 3.12.13 / MCP 1.30.0: 1902 passed, 9 skipped (two pre-existing
  resource-return deprecation warnings).
- Python 3.13.15 / MCP 2.3.0: 1905 passed, 6 skipped.
- Both SDKs: real stdio process, ClientSession initialization, tool discovery,
  keyframe writes and output readback. Thirteen tool groups expose the new
  actions; source inputs are preserved and output passes the local Apple DTD.
- Apple FCPXML 1.10 and 1.14 DTDs used from the installed application.
- Independent review used the FCP-exported XML, tested exact source-time
  mapping, invalid-value atomicity, property isolation, retimed rejection and
  readback against Apple 1.14 DTD. A follow-up independent review confirmed
  name-versus-key matching, disabled-parameter refusal and static-value
  precedence after their regression fixes.

The full suite is run with `python -m pytest tests/ -q`; lint is
`ruff check . --exclude docs/`. New tests live in `tests/test_keyframes.py`
and `tests/test_keyframe_tools.py`. Native FCP roundtrip test data is reduced
to parameter nodes, excluding bookmarks and local media paths.

## Actual Final Cut Pro 12.4 observations

A 12-second 1280×720/30 fps test pattern plus 440 Hz audio was generated with
FFmpeg. The test sequence uses six seconds from source time 5 seconds. New
MCP batch operations placed two points per property at relative 0 and 5
seconds (XML source time 5 and 10 seconds).

| Property | Endpoints | FCP Inspector at relative 2.5 s |
|---|---|---|
| Position | `[0,0]` → `[10,5]` | `(36,18)` px, matching `(5,2.5)` native units at 720p |
| Scale | `1` → `1.2` | 110% |
| Rotation | `0` → `15` | 7.5° |
| Opacity | `0.25` → `1` | 62.5% |
| Volume | `-24` → `0` dB | -5.5 dB (linear gain, not linear dB) |

FCP re-export preserved both times and all ten endpoint values. It also
established the native interpolation encoding: position/scale/opacity use
`curve="linear"`; rotation/volume omit `interp` and `curve`. The initial
uniform `interp="linear"` encoding caused import warnings for position,
scale and volume even though its DTD check passed. The implementation and
regression fixtures use the native encoding instead.

The corrected file was generated and read back through real MCP 1.x stdio
and passed the Apple DTD. In Final Cut Pro 12.4, the uniquely named
`Keyframes Final QA` project imported into the separate validation library
without warnings (warning suppression was off). The midpoint values above
were confirmed again; at 5 seconds the Inspector showed position (72,36) px,
scale 120%, rotation 15 degrees, opacity 100% and volume 0 dB. Playback ran
to the final frame and stopped. The final FCP 1.14 export preserved all ten
endpoint values and their exact rational times across all five properties.
It passed Apple 1.14 DTD validation. The machine-readable comparison is
`.local-validation/keyframes/final-roundtrip.json` (local artifact).

Local media, raw exported XML, MCP reports and the isolated FCP library are
kept under the gitignored `.local-validation/keyframes/` directory. Proxy
renders are not evidence for these effects because the upstream renderer
ignores parameter animation. No subjective audio listening acceptance is
claimed.

## Reproduce with Final Cut Pro

1. Generate a synthetic media asset; create a matching-rate FCPXML sequence
   with a nonzero source in-point.
2. Call `inspect.list_keyframes`, then `edit.batch_keyframes` with the values
   above, using unique paths from that exact input.
3. Check the returned Apple DTD status and read the new file back.
4. Import to a new, separate FCP library with warnings visible. Open the test
   project and inspect the start, midpoint and endpoint values.
5. Export XML from FCP and compare the five parameter curves, rational
   source times and values. Re-inspect this export before further edits.
