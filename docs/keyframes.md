# Video and audio keyframes

Use `inspect.list_keyframes` to read animation, and `edit.set_keyframes`,
`edit.delete_keyframes` or `edit.batch_keyframes` to change it. These actions
belong to the existing `inspect` and `edit` tool groups.

## Run this source version

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python server.py
```

Configure a client to launch the checkout's absolute `.venv/bin/python` with
its absolute `server.py` path as an argument. Keep the existing project/read/
write roots appropriate for that client. A client restart/reconnect is needed
before its tool inventory includes these additions.

## Parameters and time

| Property | Values | Example |
|---|---|---|
| `position` | Two native FCPXML height-percentage coordinates `[x,y]`; positive y is up | `[10,5]` = 72 px right, 36 px up in the verified 720p project |
| `scale` | Positive multiplier or `[x,y]` multipliers | `1.2` = 120% |
| `rotation` | Degrees, positive counterclockwise | `15` |
| `opacity` | Fraction in `[0,1]` | `0.5` = 50% |
| `volume` | Numeric dB, not an amplitude multiplier | `-12` |

Each point contains `value` and exactly one of:

- `time`: nonnegative rational seconds relative to the visible clip start,
  such as `"0s"`, `"5s"` or `"1001/30000s"`.
- `frame`: nonnegative integer frame index in the containing project.

Times must satisfy `0 <= time < duration`. Use `frame` for exact project-frame
placement; rational `time` can represent subframe audio automation.
For a six-second 30 fps clip, frames 0–179 are valid; frame 180 is outside.
A clip `start` of 5 seconds plus a relative point at 2 seconds becomes
`time="7s"` in XML. Intrinsic animation uses the clip's **local output clock**,
including on retimed clips. Do not pass that animation time through `timeMap`
when writing XML. Fractions are preserved for 23.976/29.97 frame rates.
There is no floating-point time input or SMPTE string guessing.

Inspection returns `time` relative to the visible clip start and `local_time`
as the raw XML keyframe time. The earlier `source_time` field remains as a
compatibility alias of `local_time`; it does not identify the media source
clock on retimed clips. `mapped_source_time` reports that separate media clock
through a supported linear `timeMap`, or `null` when unavailable. Retained
points outside the visible clip still have their original local times and
`in_range: false`; no source-map extrapolation is guessed.

This distinction was verified with FCP 12.4 using a clip starting at 5 seconds,
a 2x time map, and animation keys at local times 5 and 10 seconds. At output
1.5 seconds, the animation is 30% between those keys (local time 6.5 seconds),
while the media has reached source time 8 seconds. Both video and volume use
this local output clock.

New curves use native linear behavior. Optional `interp` / `curve` inputs
accept only `"linear"`; omit them to preserve an existing point's attributes
when changing its value. The XML representation differs by parameter:
position/scale/opacity write `curve="linear"`; rotation/volume omit these
attributes, as verified in Final Cut Pro 12.4 exports. Volume interpolation
is linear in gain, **not in dB**: halfway from -24 dB to 0 dB reads about
-5.5 dB in FCP. Automatic speech detection/ducking is not included; provide
the times and levels for the desired envelope.

## Select the exact clip

Call `inspect` first:

```json
{"action":"list_keyframes","args":{"filepath":"/path/project.fcpxml"}}
```

Copy a `clip_path` from the returned `clips`. It identifies a specific XML
occurrence, even when names are identical. Never construct it from a name.
Inspect the returned `supported`, `unsupported_reasons` and per-property
status. Re-inspect after structural edits or a new export: paths refer to the
current document, not persistent FCP identities.

## Set and merge

Call `edit` with the path returned above:

```json
{
  "action":"set_keyframes",
  "args":{
    "filepath":"/path/project.fcpxml",
    "clip_path":"/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]/asset-clip[1]",
    "property":"scale",
    "mode":"merge",
    "keyframes":[{"frame":0,"value":1},{"frame":150,"value":1.2}]
  }
}
```

`merge` updates matching times and retains other points, including points
outside the current trimmed range. `replace` replaces only the selected
property's animation. Neither operation rebuilds the whole project from the
lossy parser models. Other adjustments, markers, effects and bundle sidecars
are retained. Existing disabled/ambiguous parameters or unsupported curve
payloads are reported instead of silently rewritten.

## Delete or batch

`delete_keyframes` takes `filepath`, `clip_path`, `property` and optional
`times` (rational seconds). Omit `times` to remove that property's animation;
its underlying static parameter/adjustment value remains.

`batch_keyframes` takes one `filepath` and an `operations` array. Each item
uses `action: "set"` or `"delete"` plus the fields above, without its own
filepath/output path. It applies up to 100 operations, up to 1,000 points per
operation and 10,000 total, then saves once. Any invalid item aborts without
publishing output. This can animate several properties or clips together.

## Supported scope and output

Editable targets are ordinary `asset-clip`, `video` and `audio` elements in a
project timeline, including one ordinary connected-clip layer with an
unretimed host. The target may have a strictly increasing linear `timeMap`
covering its full visible duration. Source and project frame rates must match.
Explicit `conform-rate scaleEnabled="0"` is accepted only with matching rates.
Reverse/freeze/smooth time maps, retimed hosts, active rate conform, uncertain frame
conversion, compound/ref/multicam/sync/title structures and arbitrary effect
parameters are rejected for editing and exposed with reasons. Unsupported
maps remain inspectable: local keyframe times and values are retained, while
`mapped_source_time` is unavailable. No custom Bezier editor or tracking is
provided. After trimming, splitting or changing speed, re-inspect the visible
range and verify in FCP. See [speed curve editing](retiming.md).

Outputs are new numbered `_keyframes` siblings by default. Explicit
`output_path` must pass the existing source-directory write policy and must
not exist; in-place input/bundle writes are refused. XML is staged and checked
against Apple's DTD when available before publication. A failed DTD check
publishes nothing; a machine without Apple DTDs reports `unavailable` rather
than claiming validation. Bundle sidecars are copied and unsuccessful copies
are cleaned up. Successful writes participate in the existing operation
journal and undo workflow.

DTD validity proves XML structure, not FCP behavior. The proxy renderer does
not evaluate these animations. Import into a separate FCP test library,
inspect start/middle/end values, then export XML and compare time/value
curves. See [validation evidence](keyframes-validation.md).

## Sources

- [Apple: Animation](https://developer.apple.com/documentation/professional-video-applications/animation)
- [Apple: Adjustment attributes and effect parameters](https://developer.apple.com/documentation/professional-video-applications/adjustment-attributes-and-effect-parameters)
- The Apple DTDs installed with Final Cut Pro (located at runtime, not redistributed).
