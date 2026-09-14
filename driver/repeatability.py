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

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real
from statistics import median

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

# Public comparisons are cheap, but they still need explicit resource bounds:
# traces may eventually arrive through a request and resampling is controlled
# by the caller. The recorder currently keeps only 300 points, so these limits
# leave ample room for imported evidence without accepting unbounded work.
MAX_TRACE_POINTS = 100_000
MAX_COMPARISON_SAMPLES = 10_000
MAX_WIDTH_PX = 100_000
MIN_OVERLAP_SECONDS = 0.25
MIN_COVERAGE = 0.95
MAX_GAP_CADENCE_MULTIPLE = 4.0

EVIDENCE_SCOPE = (
    "angular trace estimate only; assumes the stated horizontal FOV, ignores "
    "translation, roll, lens distortion and scene change, and is not proof "
    "of an actual composite"
)


class RepeatabilityError(ValueError):
    """Two takes that cannot be meaningfully compared."""


def degrees_per_pixel(fov_deg: float = DEFAULT_FOV_DEG,
                      width_px: int = DEFAULT_WIDTH_PX) -> float:
    if (isinstance(fov_deg, bool) or not isinstance(fov_deg, Real)
            or not math.isfinite(fov_deg) or not 0 < fov_deg < 180):
        raise RepeatabilityError("field of view must be finite and between 0 and 180 degrees")
    if (isinstance(width_px, bool) or not isinstance(width_px, int)
            or not 0 < width_px <= MAX_WIDTH_PX):
        raise RepeatabilityError(
            f"width must be an integer from 1 to {MAX_WIDTH_PX} pixels")
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
    overlap_seconds: float = 0.0
    coverage_a: float = 1.0
    coverage_b: float = 1.0
    max_gap_a_seconds: float = 0.0
    max_gap_b_seconds: float = 0.0
    quality_reasons: tuple[str, ...] = ()
    evidence_scope: str = EVIDENCE_SCOPE

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
        if self.quality_reasons:
            return "partial comparison"
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
            "evidence_scope": self.evidence_scope,
            "quality_reasons": list(self.quality_reasons),
            "overlap_seconds": round(self.overlap_seconds, 3),
            "coverage_a": round(self.coverage_a, 4),
            "coverage_b": round(self.coverage_b, 4),
            "max_gap_a_seconds": round(self.max_gap_a_seconds, 3),
            "max_gap_b_seconds": round(self.max_gap_b_seconds, 3),
        }


def _validated_trace(trace: object, name: str) -> list[tuple[float, float, float]]:
    """Return a bounded, normalized trace or refuse the complete input.

    No point is discarded: filtering malformed telemetry would make a damaged
    take look more complete than the evidence supports.
    """
    if isinstance(trace, (str, bytes)):
        raise RepeatabilityError(f"take {name} trace must be a sequence of points")
    try:
        points = iter(trace)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RepeatabilityError(
            f"take {name} trace must be a sequence of points") from exc

    result: list[tuple[float, float, float]] = []
    previous_t: float | None = None
    for index, point in enumerate(points):
        if index >= MAX_TRACE_POINTS:
            raise RepeatabilityError(
                f"take {name} trace exceeds {MAX_TRACE_POINTS} points")
        if (not isinstance(point, Sequence)
                or isinstance(point, (str, bytes)) or len(point) != 3):
            raise RepeatabilityError(
                f"take {name} point {index} must be an exact triple")
        values = tuple(point)
        if any(isinstance(value, bool) or not isinstance(value, Real)
               or not math.isfinite(value) for value in values):
            raise RepeatabilityError(
                f"take {name} point {index} must contain finite real numbers")
        t, pitch, yaw = (float(value) for value in values)
        if t < 0:
            raise RepeatabilityError(f"take {name} times must be nonnegative")
        if previous_t is not None and t <= previous_t:
            raise RepeatabilityError(
                f"take {name} times must be strictly increasing")
        result.append((t, pitch, yaw))
        previous_t = t

    if len(result) < 2:
        raise RepeatabilityError("both takes need a trace of at least two points")
    return result


def _duration(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or value <= 0):
        raise RepeatabilityError(f"duration for take {name} must be positive and finite")
    return float(value)


def _max_gap(trace: list[tuple[float, float, float]]) -> tuple[float, bool]:
    gaps = [right[0] - left[0] for left, right in zip(trace, trace[1:])]
    largest = max(gaps)
    cadence = median(gaps)
    return largest, largest > cadence * MAX_GAP_CADENCE_MULTIPLE


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
            samples: int = 200, *,
            duration_a: float | None = None,
            duration_b: float | None = None,
            aborted_a: bool = False,
            aborted_b: bool = False) -> Comparison:
    """Estimate angular agreement between two measured takes.

    Pixel figures are a linear angular-to-pixel estimate using the supplied
    horizontal FOV. They ignore translation, roll, distortion and scene
    changes, so even a quality-passing result cannot prove a real composite.
    """
    # Validate comparison controls before doing any trace work or arithmetic.
    degrees_per_pixel(fov_deg, width_px)
    if (isinstance(samples, bool) or not isinstance(samples, int)
            or not 2 <= samples <= MAX_COMPARISON_SAMPLES):
        raise RepeatabilityError(
            f"samples must be an integer from 2 to {MAX_COMPARISON_SAMPLES}")
    expected_a = _duration(duration_a, "A")
    expected_b = _duration(duration_b, "B")
    if not isinstance(aborted_a, bool) or not isinstance(aborted_b, bool):
        raise RepeatabilityError("aborted context must be boolean")

    a = _validated_trace(trace_a, "A")
    b = _validated_trace(trace_b, "B")

    # Compare over the overlap only. A take that was stopped early has no
    # opinion about the part it never shot, and treating its held final value
    # as a measurement would invent a huge disagreement.
    end = min(a[-1][0], b[-1][0])
    start = max(a[0][0], b[0][0])
    if end <= start:
        raise RepeatabilityError("the two takes do not overlap in time")

    n = samples
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

    overlap = end - start
    union_span = max(a[-1][0], b[-1][0]) - min(a[0][0], b[0][0])

    def coverage(expected: float | None) -> float:
        if expected is None:
            return min(1.0, overlap / union_span)
        covered = max(0.0, min(end, expected) - max(start, 0.0))
        return min(1.0, covered / expected)

    coverage_a = coverage(expected_a)
    coverage_b = coverage(expected_b)
    max_gap_a, gapped_a = _max_gap(a)
    max_gap_b, gapped_b = _max_gap(b)
    reasons: list[str] = []
    if aborted_a:
        reasons.append("take A was aborted")
    if aborted_b:
        reasons.append("take B was aborted")
    if overlap < MIN_OVERLAP_SECONDS:
        reasons.append("overlap is shorter than 0.25 seconds")
    if coverage_a < MIN_COVERAGE:
        reasons.append("take A coverage is below 95%")
    if coverage_b < MIN_COVERAGE:
        reasons.append("take B coverage is below 95%")
    if gapped_a:
        reasons.append("take A has an unusually large interpolation gap")
    if gapped_b:
        reasons.append("take B has an unusually large interpolation gap")

    return Comparison(samples=n, peak_pitch_deg=peak_p, peak_yaw_deg=peak_y,
                      rms_deg=rms, peak_at=peak_at,
                      fov_deg=fov_deg, width_px=width_px,
                      overlap_seconds=overlap,
                      coverage_a=coverage_a, coverage_b=coverage_b,
                      max_gap_a_seconds=max_gap_a,
                      max_gap_b_seconds=max_gap_b,
                      quality_reasons=tuple(reasons))
