# Speed curves

Use `inspect.list_speed_points`, `edit.set_speed_curve`, and
`edit.reset_speed`. To run from a source checkout, follow the installation
in [keyframes.md](keyframes.md) and reconnect the MCP client.

## Set a curve

First call `inspect` with `action: "list_speed_points"` and a `filepath`.
Select an exact `clip_path` from the result. Duplicate clip names are safe.
Then call `edit`:

```json
{
  "action": "set_speed_curve",
  "args": {
    "filepath": "/path/to/project.fcpxml",
    "clip_path": "/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]/asset-clip[1]",
    "speed_keyframes": [
      {"time": "0s", "speed": 1, "transition": "linear"},
      {"time": "2s", "speed": 2, "transition": "linear"},
      {"time": "4s", "speed": 1}
    ],
    "ripple": true,
    "preserve_pitch": true,
    "frame_sampling": "floor"
  }
}
```

This creates a four-second output clip consuming six seconds of source:
normal speed rises to 2x at two seconds, then returns to normal at four
seconds. With a source in-point of five seconds, source positions at output
0, 1, 2, 3 and 4 seconds are 5, 6.25, 8, 9.75 and 11 seconds.

Each control point takes exactly one of `time` (rational seconds such as
`1001/30000s`) or `frame` (integer project-frame index). All points must align
to project frames, begin at zero, and increase strictly. The final point's
time is the new duration. Speeds must be finite numbers, `0 < speed <= 100`.
A point's `transition` applies to the interval leading to the next point:

- `linear` (default): the speed changes linearly; its integral defines source
  progress. The map is sampled at every project frame using exact rational
  arithmetic. At 30 fps, a four-second varying curve emits 121 native map
  points. FCP displays these as many short speed segments. This is a sampled
  ramp, not a sparse native `smooth2`/Bezier curve; subframe speed is constant
  within each sampled segment.
- `hold`: maintain the point's speed until the next point, then jump to its
  speed. Constant spans stay compact. The final point supplies the endpoint
  speed for a preceding linear ramp; it creates no additional duration.

At most 1,000 input points and 10,000 emitted map points are accepted. Times
must fit FCPXML's signed 64-bit numerator and unsigned 32-bit denominator;
use simple speed values when excessive decimal precision is rejected.

## Duration, source range, and audio

The source in-point is preserved when replacing a speed curve. The integral
of the requested speeds determines the new source out-point, which can be
before or after the previous trim but must remain inside the media asset.
Read `source_consumed` and `source_trim_change` in the result. There is no
implicit normalization of speeds to preserve the previous source out-point.

`ripple: true` shifts subsequent primary-storyline offsets by the duration
change and updates sequence duration. Attached items on later clips move
with their hosts. Ambiguous extents, overlaps, transitions and unsupported
timed structures are refused. `ripple: false` requires unchanged duration.

The same native time map retimes the enabled audio/video components.
`preserve_pitch` explicitly writes FCP's audio pitch-preservation flag.
`frame_sampling` selects `floor`, `nearest-neighbor`, `frame-blending`,
`optical-flow-classic`, or `optical-flow`; processing occurs inside FCP.
No proxy render is claimed to verify variable speed or pitch quality.

## Existing animation and clock semantics

Intrinsic position, scale, rotation, opacity and volume animation use the
**adjusted local clip clock**, even when a time map is present. Their XML
keyframe time is `clip.start + clip-relative output time`. Media source time
is separately obtained by applying the time map. This was verified in FCP,
including nonzero in-points and both video and audio parameters.

Setting a speed curve preserves intrinsic keyframe times and values. The
animation keeps its output timing and does not follow individual source
frames. Shortening a clip may hide later keys; those keys are retained.
Existing `set_keyframes`, `delete_keyframes`, and `batch_keyframes` can edit
clips carrying supported forward linear maps. Inspect results expose
`local_time` and `mapped_source_time`; `source_time` remains a legacy alias
for raw XML keyframe time, for compatibility.

## Inspect and reset

`list_speed_points` reports exact native local/source map points and the
outgoing segment's `speed` and rational `speed_ratio`, plus unsupported
reasons and raw maps. These are actual XML points, including any outside the
visible trim. They do not reconstruct the author's original control points
from a sampled ramp or FCP's normalized export.

To remove the curve, call `edit` with `action: "reset_speed"`, `filepath`,
`clip_path`, and optional `ripple`. It restores 1x over the currently consumed
source range. Source in/out points snap to the nearest source frame (at most
half a frame per endpoint); any adjustment is returned exactly with a warning.
This handles small timing quantization in FCP exports. The operation checks
asset bounds after snapping and shifts intrinsic XML keyframe times when
needed to keep their clip-relative positions. Resetting speed is not undo of
an earlier source trim; use the operation journal's `undo` for file-level undo.

## Supported scope and output

Speed editing currently supports ordinary primary-storyline `asset-clip`,
`video`, and `audio` elements with explicit asset bounds and a known matching
video/project frame rate (audio-only sources need no video frame rate).
Existing forward, strictly increasing maps with explicit `interp="linear"`
are supported. Disabled rate conform is allowed only when mapping is proven.

Reverse playback, freeze frames, native `smooth2` maps, implicit/enabled rate
conform, compound/multicam/sync clips, split A/V ranges, targets with attached
clips or timed annotations, arbitrary animated effects, and tracking remain
inspectable but are refused for edits. Existing upstream `change_speed` is
unchanged; use the new actions for the guarded speed/animation workflow.

Outputs are new numbered `_retimed` siblings. Explicit output paths follow
the existing input-directory write policy and must not exist. Bundle sidecars
are copied. Edits are validated before mutation, saved to staging, checked
against the available Apple DTD, then published without overwriting. Missing
DTDs are reported as unavailable. A failed operation publishes no output and
creates no successful-output journal entry.

See [validation evidence](retiming-validation.md) for automated tests and
actual Final Cut Pro import/playback/export observations.
