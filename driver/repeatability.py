"""Did take 3 actually match take 1?

This is the question motion control exists to answer. A rig that repeats a
move is only useful if you can prove it repeated: a plate pass and a clean
pass composite only when the camera was in the same place at the same moment
in both, and "it looked the same on the monitor" is not proof of anything.

The comparison is between the MEASURED traces of two takes, not between their
commanded paths. The commanded path is identical by construction -- both takes
were told the same thing -- so comparing it proves nothing at all.

Degrees are the wrong unit for the verdict. What matters is whether the
difference shows up in the frame, and that depends on the lens: half a degree
is nothing on a wide shot and a badly misaligned composite on a long one. So
the answer is given in PIXELS, with the field of view stated, and the field of
view is an assumption the operator can correct rather than a measurement this
code can make.
"""

from __future__ import annotations

from dataclasses import dataclass

from .moves import wrap180

# Horizontal field of view for the Pocket's own lens at 1x, in degrees.
#
# ASSUMED, not measured: the camera does not report its field of view over any
# command implemented here. It is roughly right for a 20mm-equivalent lens on
# 16:9 and is the correct order of magnitude, but anyone shooting through
# added glass or at a different zoom should set their own. Every result that
# depends on it is labelled with the value used.
DEFAULT_FOV_DEG = 84.0
DEFAULT_WIDTH_PX = 1920

# A composite survives roughly a pixel of drift before anyone notices; two is
# usually fixable with a stabilise pass; beyond that the takes do not match.
COMPOSITE_PX = 1.0
FIXABLE_PX = 3.0


class RepeatabilityError(ValueError):
    """Two takes that cannot be meaningfully compared."""


def degrees_per_pixel(fov_deg: float = DEFAULT_FOV_DEG,
                      width_px: int = DEFAULT_WIDTH_PX) -> float:
    if fov_deg <= 0 or width_px <= 0:
        raise RepeatabilityError("field of view and width must be positive")
    return fov_deg / width_px


@dataclass(frozen=True)
class Comparison:
    samples: int
    peak_pitch_deg: float
    peak_yaw_deg: float
    rms_deg: float
    peak_at: float
    fov_deg: float
    width_px: int

    @property
    def peak_deg(self) -> float:
        return max(self.peak_pitch_deg, self.peak_yaw_deg)

    @property
    def peak_px(self) -> float:
        return self.peak_deg / degrees_per_pixel(self.fov_deg, self.width_px)

    @property
    def rms_px(self) -> float:
        return self.rms_deg / degrees_per_pixel(self.fov_deg, self.width_px)

    @property
    def verdict(self) -> str:
        px = self.peak_px
        if px <= COMPOSITE_PX:
            return "matched"
        if px <= FIXABLE_PX:
            return "needs stabilise"
        return "no match"

    @property
    def compositable(self) -> bool:
        return self.verdict == "matched"

    def summary(self) -> str:
        return (f"{self.verdict}: peak {self.peak_deg:.3f} deg "
                f"({self.peak_px:.1f} px at {self.fov_deg:.0f} deg FOV) "
                f"at {self.peak_at:.1f}s")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "compositable": self.compositable,
            "samples": self.samples,
            "peak_deg": round(self.peak_deg, 4),
            "peak_pitch_deg": round(self.peak_pitch_deg, 4),
            "peak_yaw_deg": round(self.peak_yaw_deg, 4),
            "peak_px": round(self.peak_px, 2),
            "rms_deg": round(self.rms_deg, 4),
            "rms_px": round(self.rms_px, 2),
            "peak_at": round(self.peak_at, 2),
            "fov_deg": self.fov_deg,
            "width_px": self.width_px,
            "fov_assumed": True,
        }


def _at(trace: list, t: float) -> tuple[float, float] | None:
    """Linear interpolation into a trace at time `t`.

    Traces from two takes are not sampled at the same instants -- the runner
    ticks on its own clock and a cued take pauses -- so comparing them
    index-by-index would report timing jitter as positional error. Both are
    resampled onto a common timeline instead.
    """
    if not trace:
        return None
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


def compare(trace_a: list, trace_b: list,
            fov_deg: float = DEFAULT_FOV_DEG,
            width_px: int = DEFAULT_WIDTH_PX,
            samples: int = 200) -> Comparison:
    """How closely two takes of the same move actually agreed."""
    a = [tuple(p) for p in trace_a if len(p) >= 3]
    b = [tuple(p) for p in trace_b if len(p) >= 3]
    if len(a) < 2 or len(b) < 2:
        raise RepeatabilityError("both takes need a trace of at least two points")

    # Compare over the overlap only. A take that was stopped early has no
    # opinion about the part it never shot, and treating its held final value
    # as a measurement would invent a huge disagreement.
    end = min(a[-1][0], b[-1][0])
    start = max(a[0][0], b[0][0])
    if end <= start:
        raise RepeatabilityError("the two takes do not overlap in time")

    n = max(2, int(samples))
    peak_p = peak_y = 0.0
    peak_at = start
    sq = 0.0
    for i in range(n):
        t = start + (end - start) * i / (n - 1)
        pa, pb = _at(a, t), _at(b, t)
        if pa is None or pb is None:
            continue
        dp = abs(wrap180(pa[0] - pb[0]))
        dy = abs(wrap180(pa[1] - pb[1]))
        if max(dp, dy) > max(peak_p, peak_y):
            peak_at = t
        peak_p = max(peak_p, dp)
        peak_y = max(peak_y, dy)
        sq += dp * dp + dy * dy

    rms = (sq / (2 * n)) ** 0.5
    return Comparison(samples=n, peak_pitch_deg=peak_p, peak_yaw_deg=peak_y,
                      rms_deg=rms, peak_at=peak_at,
                      fov_deg=fov_deg, width_px=width_px)
