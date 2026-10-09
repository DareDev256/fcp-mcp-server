# Video and audio keyframes

Use `inspect.list_keyframes` to read animation, and `edit.set_keyframes`,
`edit.delete_keyframes`, `edit.batch_keyframes` or `edit.set_animation_curve`
to change it. These actions
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

New points default to native linear behavior. `interp` accepts `linear` on all
properties and native `ease`, `easeIn`, `easeOut` on **opacity only**. Native
opacity easing keeps the authored point count. Do not combine nonlinear
`interp` with `curve`. Optional `curve` accepts only `linear`; `smooth` is not
exposed because a smooth tag alone did not produce curved motion in FCP 12.4.
Omit both fields to preserve existing attributes when changing a point's value.
Explicit linear normalizes them: position/scale/opacity use `curve="linear"`;
rotation/volume omit both fields. Native opacity easing uses `interp` alone.

Volume interpolation is linear in gain, **not in dB**: halfway from -24 dB to
0 dB reads about -5.5 dB in FCP. Audio automation remains on `set_keyframes`;
automatic speech detection/ducking is not included.

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

## Bezier paths and temporal easing

Use `edit.set_animation_curve` for a curved motion path or eased transform.
It accepts 2–100 authored `points` and **replaces the selected property's whole
animation**, including old out-of-range keys. Other properties remain intact.
Each point has `value` and exactly one project-aligned `time` or `frame`; times
must be strictly increasing and inside the visible clip. Unlike ordinary audio
keyframes, this action does not accept subframe times.

Optional `control_out` and `control_in` describe a cubic Bezier segment using
**absolute property values**, not offsets or time coordinates. Missing handles
are placed at one-third and two-thirds along the straight chord. Supply handles
to bend a position path. First `control_in`, last `control_out` and last `easing`
are rejected because they do not belong to a segment. Controls use the same
shape and limits as values (including positive scale and opacity in `[0,1]`).
Values and tolerances must be finite, with absolute value at most `1e12`.

Each segment starts at a point whose optional `easing` transforms normalized
time `u`: `linear` = `u`; `ease` = `3u²−2u³`; `easeIn` = `u²`; `easeOut` =
`2u−u²`. This controls travel along the cubic, so easing changes motion even
when the spatial path is straight. These are sampler formulas, not claims that
FCP's native opacity presets use the same formulas.

Example: a five-second arch in a clip longer than five seconds:

```json
{
  "action": "set_animation_curve",
  "args": {
    "filepath": "/path/project.fcpxml",
    "clip_path": "/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]/asset-clip[1]",
    "property": "position",
    "points": [
      {"time": "0s", "value": [-40, 0], "control_out": [-20, 40], "easing": "ease"},
      {"time": "5s", "value": [40, 0], "control_in": [20, 40]}
    ],
    "tolerance": 0.05
  }
}
```

The action evaluates the cubic at project frames, then selects keys two
ways: reaching forward from each kept key as far as the straight segment
stays within tolerance, and repeatedly adding the worst frame of each
segment. It keeps whichever selection needs fewer keys. Both check
**same-time** interpolation error at every evaluated frame and preserve
every authored knot. It reports `authored_point_count`,
`sampled_frame_count`, `generated_keyframe_count`, `tolerance`, `max_error` and
`representation: "adaptive_linear"`. The key count is usually at or near the
minimum for the requested accuracy, but the minimum is not guaranteed.
Straight uniform motion can use just its endpoints.

`tolerance` is absolute scalar error or Euclidean vector error in property
units. Defaults are position `0.05`, scale `0.001`, rotation `0.1`, opacity
`0.001`. At 720p, position `0.05` corresponds to `0.36` pixels. Smaller tolerances
usually retain more keys. The guarantee covers project frame times between
authored endpoints, before FCP serialization; it does not cover between-frame
samples, media retiming, or native export rounding. Limits are 18,000 evaluated
frames and 1,000 generated keys; exceeding a limit fails without publishing.

The output contains ordinary **linear FCP keyframes approximating the authored
curve**, not native Bezier handles. Save the request's control points to reauthor
the curve; XML readback describes the generated keys and cannot reconstruct
those controls. `set_animation_curve` supports position, scale, rotation and
opacity, not volume or `timeMap` speed curves. For sparse native opacity easing,
prefer `set_keyframes` with the opacity `interp` presets.

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
`mapped_source_time` is unavailable. Native spatial Bezier handles and tracking
are not exposed. After trimming, splitting or changing speed, re-inspect the visible
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

See [curve-specific validation](animation-curves-validation.md) for native easing
and adaptive Bezier import/export evidence.

## Sources

- [Apple: Animation](https://developer.apple.com/documentation/professional-video-applications/animation)
- [Apple: Adjustment attributes and effect parameters](https://developer.apple.com/documentation/professional-video-applications/adjustment-attributes-and-effect-parameters)
- The Apple DTDs installed with Final Cut Pro (located at runtime, not redistributed).
