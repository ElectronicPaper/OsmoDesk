"""Get the camera move out of this rig and into post.

A take log tells a human what was shot. This exports what the camera actually
DID, frame by frame, in formats a tracking or compositing package will read:
Nuke/Maya `.chan`, and a CSV for everything else.

Two things make this honest rather than decorative.

**Measured, not commanded.** The default source is the take's recorded trace --
where the head actually was -- not the path it was told to follow. Those differ
by the tracking error, and the tracking error is exactly what a compositor is
trying to account for. Exporting the command would hand them a file that is
right about everything except the part they need.

**The frame of reference is stated, not assumed.** Level is NOT zero on this
head: pitch travel runs +64.5 deg through +/-180, so a raw export drops the
camera into post at an orientation that is meaningless there. By default the
export is RELATIVE to the first frame, which is what a match-move actually
wants -- the compositor places frame one and the file supplies the motion from
there. An absolute export is available but requires the operator to say which
gimbal pitch is level, because this code cannot know.

`.chan` is a plain text table, one line per frame:

    frame  TX TY TZ  RX RY RZ  [VFOV]

A pan/tilt head produces no translation, so TX/TY/TZ are zero and stay zero.
That is not a gap in the export -- it is the truth about the rig, and it is
the reason this camera data is cheap for a compositor to use and also why it
is worth less than a move with real parallax.
"""

from __future__ import annotations

from .moves import wrap180

# Nuke's default rotation order is ZXY, and for a pan/tilt head the mapping is
# unambiguous: tilt turns about the horizontal axis (X), pan about the vertical
# (Y), and nothing rolls (Z).
ROTATION_ORDER = "ZXY"

DEFAULT_FPS = 25.0

# Vertical field of view written into the .chan when one is asked for. Nuke
# reads column 8 as VFOV in degrees. Derived from the horizontal assumption
# used elsewhere in the driver, and equally an assumption -- the camera does
# not report it.
DEFAULT_VFOV_DEG = 52.0


class ExportError(ValueError):
    """A trace that cannot be exported."""


def _clean(trace) -> list[tuple[float, float, float]]:
    out = []
    for p in trace or ():
        if len(p) >= 3:
            out.append((float(p[0]), float(p[1]), float(p[2])))
    if len(out) < 2:
        raise ExportError("a path export needs at least two trace points")
    out.sort(key=lambda p: p[0])
    return out


def _at(trace, t: float) -> tuple[float, float]:
    """Interpolate the trace at time `t`, the short way round in angle."""
    if t <= trace[0][0]:
        return (trace[0][1], trace[0][2])
    if t >= trace[-1][0]:
        return (trace[-1][1], trace[-1][2])
    lo, hi = 0, len(trace) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if trace[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, p0, y0 = trace[lo]
    t1, p1, y1 = trace[hi]
    span = t1 - t0
    s = 0.0 if span <= 0 else (t - t0) / span
    return (p0 + wrap180(p1 - p0) * s, y0 + wrap180(y1 - y0) * s)


def resample(trace, fps: float = DEFAULT_FPS) -> list[tuple[int, float, float]]:
    """Put the trace on a frame grid.

    A trace is sampled on the runner's own clock, which is neither the camera's
    frame clock nor evenly spaced once decimation has been through it. Post
    works in frames, so it is resampled here rather than leaving a compositor
    to guess what the timestamps meant.
    """
    if fps <= 0:
        raise ExportError("frame rate must be positive")
    pts = _clean(trace)
    t0, t1 = pts[0][0], pts[-1][0]
    total = t1 - t0
    frames = max(1, int(round(total * fps)))
    out = []
    for i in range(frames + 1):
        t = t0 + i / fps
        pitch, yaw = _at(pts, min(t, t1))
        out.append((i + 1, pitch, yaw))
    return out


def _referenced(rows, level_pitch: float | None, level_yaw: float | None):
    """Apply the frame of reference.

    Relative by default: subtract the first frame, so the file describes the
    motion and the compositor places frame one. Absolute only when the
    operator has said what level is -- this code cannot know, and guessing
    would put the camera in post at a plausible-looking wrong orientation.
    """
    if level_pitch is None:
        level_pitch = rows[0][1]
    if level_yaw is None:
        level_yaw = rows[0][2]
    return [(n, wrap180(p - level_pitch), wrap180(y - level_yaw))
            for n, p, y in rows]


def to_chan(trace, fps: float = DEFAULT_FPS,
            level_pitch: float | None = None, level_yaw: float | None = None,
            vfov_deg: float | None = None) -> str:
    """Nuke/Maya `.chan`: frame, translation, rotation, optional VFOV."""
    rows = _referenced(resample(trace, fps), level_pitch, level_yaw)
    lines = []
    for n, pitch, yaw in rows:
        # TX TY TZ are zero and stay zero: a pan/tilt head does not translate.
        cols = [str(n), "0", "0", "0",
                f"{pitch:.6f}", f"{yaw:.6f}", "0"]
        if vfov_deg is not None:
            cols.append(f"{vfov_deg:.4f}")
        lines.append(" ".join(cols))
    return "\n".join(lines) + "\n"


def to_csv(trace, fps: float = DEFAULT_FPS,
           level_pitch: float | None = None, level_yaw: float | None = None,
           vfov_deg: float | None = None) -> str:
    """CSV with a header that states the convention.

    `.chan` has no room for a comment, so anything a consumer needs to know
    has to travel out of band. CSV does have room, and a file that does not
    say what frame its angles are in is a file someone will misread.
    """
    rows = _referenced(resample(trace, fps), level_pitch, level_yaw)
    relative = level_pitch is None and level_yaw is None
    head = [
        "# Osmo Pocket rig camera path",
        f"# fps={fps:g} rotation_order={ROTATION_ORDER}",
        "# reference=" + ("first frame (relative motion)" if relative
                          else f"absolute, level at pitch={level_pitch:g} yaw={level_yaw:g}"),
        "# tilt=rotation about X, pan=rotation about Y, roll always 0",
        "# translation is always 0: this is a pan/tilt head, it does not move",
        "# angles in degrees, MEASURED from gimbal telemetry, not commanded",
    ]
    cols = ["frame", "seconds", "tilt_deg", "pan_deg"]
    if vfov_deg is not None:
        cols.append("vfov_deg")
        head.append("# vfov is ASSUMED, not reported by the camera")
    out = head + [",".join(cols)]
    for n, pitch, yaw in rows:
        row = [str(n), f"{(n - 1) / fps:.4f}", f"{pitch:.4f}", f"{yaw:.4f}"]
        if vfov_deg is not None:
            row.append(f"{vfov_deg:.3f}")
        out.append(",".join(row))
    return "\n".join(out) + "\n"


def describe(trace, fps: float = DEFAULT_FPS) -> dict:
    """What the export will contain, without producing it."""
    rows = resample(trace, fps)
    pitches = [p for _, p, _ in rows]
    yaws = [y for _, _, y in rows]
    return {
        "frames": len(rows),
        "fps": fps,
        "duration": round((len(rows) - 1) / fps, 3),
        "tilt_range": [round(min(pitches), 3), round(max(pitches), 3)],
        "pan_range": [round(min(yaws), 3), round(max(yaws), 3)],
        "translation": "none — pan/tilt head",
        "rotation_order": ROTATION_ORDER,
    }
