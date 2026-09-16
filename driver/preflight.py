"""Does an authored path stay within the rig's sampled planning limits?

A programmed move is a promise about where the head will be at every instant.
The head can break that promise in three ways, and all three are cheap to find
here and expensive to find on set:

* the path leaves the travel arc, so the axis grinds into a soft stop and the
  move arrives somewhere it was not asked to go;
* the path asks for more degrees per second than the head can produce, so it
  falls behind and the timing of everything after that point is wrong;
* the path asks an axis for so little movement that its demand never clears
  the camera's dead band, so that axis does not move at all.

The dead-band test is deliberately about the WHOLE axis rather than about
individual samples. Every eased move starts and ends at zero velocity and so
passes through the dead band twice by construction; flagging that would fire
on every well-made move. What actually fails is an axis whose peak demand over
the entire move never clears the band -- asked to travel a third of a degree
over a minute, it simply sits there.

None of these announce themselves while authoring. A move can have every one
of its nodes inside the arc and still swing outside between them; it can look
gentle and still demand 60 deg/s across one short leg.

This module samples the path and reports what it finds. It sends nothing and
touches no hardware -- it is arithmetic over `Move.sample`, so it runs with the
camera in its case. A passing report is not physical certification: it cannot
prove command acceptance, tracking, smoothness, mounting or the resulting shot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import limits, moves, response

# 50 Hz. The stick runs at 25 Hz, so sampling twice that finds anything the
# rig could actually be commanded to do without inventing detail between
# commands it will never receive.
SAMPLE_HZ = 50.0

# Resource boundary for offline arithmetic. Every accepted leg still receives
# eight leg-relative intervals in addition to a whole-timeline pass. Larger
# authored inputs fail closed as unassessed instead of returning a misleading
# OK or turning a preview request into unbounded work.
MAX_WAYPOINTS = 400
MAX_SAMPLES = 5000
LEG_INTERVALS = 8

# A violation has to last longer than this to be worth reporting. A single
# sample a hundredth of a degree outside the arc is arithmetic noise at a
# turning point, not a move that will hit a stop.
MIN_DURATION_S = 0.04
TRAVEL_SLOP_DEG = 0.05


@dataclass(frozen=True)
class Finding:
    """One problem, aggregated over the span it lasts.

    Aggregated deliberately: a move that sits outside the arc for two seconds
    produces a hundred bad samples, and a hundred findings is a wall of text
    that hides the other two problems.
    """

    kind: str            # "travel" | "too fast" | "dead band" | "assessment"
    axis: str            # "pitch" | "yaw"
    start: float         # seconds into the move
    end: float
    worst: float         # the value at its worst point
    worst_at: float
    limit: float
    detail: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "axis": self.axis,
            "start": round(self.start, 2), "end": round(self.end, 2),
            "worst": round(self.worst, 2), "worst_at": round(self.worst_at, 2),
            "limit": round(self.limit, 2), "detail": self.detail,
        }


@dataclass(frozen=True)
class Report:
    ok: bool
    findings: list[Finding]
    duration: float
    peak_pitch_dps: float
    peak_yaw_dps: float
    samples: int

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
            "duration": round(self.duration, 2),
            "peak_pitch_dps": round(self.peak_pitch_dps, 2),
            "peak_yaw_dps": round(self.peak_yaw_dps, 2),
            "samples": self.samples,
        }

    def summary(self) -> str:
        if self.ok:
            return (f"{self.duration:.1f}s, peaks {self.peak_pitch_dps:.1f} "
                    f"/ {self.peak_yaw_dps:.1f} deg/s -- within sampled "
                    "planning limits")
        worst = self.findings[0]
        return (f"{len(self.findings)} problem"
                f"{'' if len(self.findings) == 1 else 's'}: {worst.detail}")


class _Run:
    """Accumulates consecutive bad samples into one finding."""

    def __init__(self, kind: str, axis: str, limit: float, detail: str,
                 min_duration: float = MIN_DURATION_S):
        self.kind, self.axis, self.limit, self.detail = kind, axis, limit, detail
        self.min_duration = min_duration
        self.start: float | None = None
        self.end = 0.0
        self.worst = 0.0
        self.worst_at = 0.0

    def hit(self, t: float, value: float) -> None:
        if self.start is None:
            self.start = t
            self.worst = value
            self.worst_at = t
        self.end = t
        if abs(value) > abs(self.worst):
            self.worst, self.worst_at = value, t

    def close(self, out: list[Finding]) -> None:
        if self.start is None:
            return
        if self.end - self.start >= self.min_duration:
            out.append(Finding(
                kind=self.kind, axis=self.axis, start=self.start, end=self.end,
                worst=self.worst, worst_at=self.worst_at, limit=self.limit,
                detail=self.detail.format(worst=abs(self.worst),
                                          at=self.worst_at, limit=self.limit),
            ))
        self.start = None


def _outside_by(arc: limits.Arc, angle: float) -> float:
    """Degrees beyond the usable arc, 0 if inside.

    Measured to the nearer end so the number means "how far it overshot",
    not "how far round the circle it is".
    """
    off = arc.offset(angle)
    if off <= arc.usable:
        return 0.0
    past_high = off - arc.usable
    past_low = 360.0 - off
    return min(past_high, past_low)


def _assessment_failure(detail: str, duration: float = 0.0) -> Report:
    finding = Finding("assessment", "move", 0.0, 0.0, 0.0, 0.0,
                      float(MAX_WAYPOINTS), detail)
    return Report(False, [finding], duration, 0.0, 0.0, 0)


def _sample_times(move: moves.Move, total: float,
                  sample_hz: float) -> list[float]:
    """Bounded global samples plus mandatory leg-relative samples.

    Uniform 50 Hz alone can see a 50 ms leg only two or three times, and a long
    dwell can consume an unbounded loop before that leg is ever reached. Eight
    intervals per leg inspect each authored transition in its own time scale.
    The remaining budget is spread across the full timeline, so long moves do
    not concentrate all approximation detail near their start.
    """
    times = {0.0, total}
    cursor = max(0.0, move.waypoints[0].dwell)
    times.add(min(total, cursor))
    for end in move.waypoints[1:]:
        leg_start = cursor
        duration = max(0.0, end.duration)
        for i in range(LEG_INTERVALS + 1):
            times.add(min(total, leg_start + duration * i / LEG_INTERVALS))
        cursor += duration
        times.add(min(total, cursor))
        cursor += max(0.0, end.dwell)
        times.add(min(total, cursor))

    remaining = MAX_SAMPLES - len(times)
    if remaining > 0:
        nominal = total * sample_hz
        if math.isfinite(nominal) and nominal + 1 <= remaining:
            step = 1.0 / sample_hz
            count = int(math.floor(nominal))
            times.update(min(total, i * step) for i in range(count + 1))
            times.add(total)
        elif remaining == 1:
            times.add(total / 2.0)
        else:
            times.update(total * i / (remaining - 1)
                         for i in range(remaining))
    return sorted(times)[:MAX_SAMPLES]


def check(move: moves.Move,
          pitch_arc: limits.Arc = limits.PITCH_ARC,
          yaw_arc: limits.Arc = limits.YAW_ARC,
          max_dps: float = response.MAX_DPS,
          min_dps: float = response.MIN_DPS,
          sample_hz: float = SAMPLE_HZ,
          min_duration_s: float = MIN_DURATION_S) -> Report:
    """Sample the whole move and report planning-limit violations.

    `max_dps` defaults to the head's measured full-throw rate. Pass a preset
    cap from `response.SPEED_CAPS` to ask the narrower question: can it shoot
    this move at the sensitivity the operator has selected?

    `min_duration_s` is how long a violation has to last to be worth
    reporting. A parameter rather than a constant so the threshold itself can
    be tested: as a module constant nothing exercised it, and setting it to
    zero broke no test.
    """
    normalized = {}
    for value, name in ((sample_hz, "sample_hz"),
                        (min_duration_s, "min_duration_s"),
                        (max_dps, "max_dps"), (min_dps, "min_dps")):
        try:
            got = float(value)
            finite = not isinstance(value, bool) and math.isfinite(got)
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            raise ValueError(f"{name} must be finite")
        normalized[name] = got
    sample_hz = normalized["sample_hz"]
    min_duration_s = normalized["min_duration_s"]
    max_dps = normalized["max_dps"]
    min_dps = normalized["min_dps"]
    if sample_hz <= 0 or min_duration_s < 0 or max_dps <= 0 or min_dps <= 0:
        raise ValueError("sample rate and speed limits must be positive")
    if len(move.waypoints) > MAX_WAYPOINTS:
        return _assessment_failure(
            f"move has {len(move.waypoints)} waypoints; offline preflight "
            f"supports at most {MAX_WAYPOINTS}")

    try:
        move.validate_axis_curves()
    except (ValueError, TypeError) as exc:
        return _assessment_failure(str(exc))

    total = move.total_duration
    if not math.isfinite(total):
        return _assessment_failure("move duration is not finite")
    if len(move.waypoints) < 2 or total <= 0:
        return Report(ok=True, findings=[], duration=0.0,
                      peak_pitch_dps=0.0, peak_yaw_dps=0.0, samples=0)

    mk = lambda k, a, lim, d: _Run(k, a, lim, d, min_duration_s)
    runs = {
        ("travel", "pitch"): mk("travel", "pitch", pitch_arc.usable,
                                  "pitch leaves its travel by {worst:.1f} deg at {at:.1f}s"),
        ("travel", "yaw"): mk("travel", "yaw", yaw_arc.usable,
                                "yaw leaves its travel by {worst:.1f} deg at {at:.1f}s"),
        ("too fast", "pitch"): mk("too fast", "pitch", max_dps,
                                    "pitch needs {worst:.1f} deg/s at {at:.1f}s, the head gives {limit:.1f}"),
        ("too fast", "yaw"): mk("too fast", "yaw", max_dps,
                                  "yaw needs {worst:.1f} deg/s at {at:.1f}s, the head gives {limit:.1f}"),
    }

    findings: list[Finding] = []
    peak_p = peak_y = 0.0
    travel_p = travel_y = 0.0
    observed_travel = {"pitch": (0.0, 0.0), "yaw": (0.0, 0.0)}
    peak_at = {"pitch": 0.0, "yaw": 0.0}
    samples = 0
    prev: tuple[float, float] | None = None
    previous_t: float | None = None
    sample_times = _sample_times(move, total, sample_hz)
    for t in sample_times:
        point = move.sample(t)
        if point is None:
            return _assessment_failure(f"path has no sample at {t:.3f}s", total)
        samples += 1
        pitch, yaw = point
        if not all(math.isfinite(v) for v in (pitch, yaw)):
            return _assessment_failure(f"path has an invalid angle at {t:.3f}s", total)

        for axis, arc, angle in (("pitch", pitch_arc, pitch),
                                 ("yaw", yaw_arc, yaw)):
            over = _outside_by(arc, angle)
            run = runs[("travel", axis)]
            if over > TRAVEL_SLOP_DEG:
                run.hit(t, over)
                if over > observed_travel[axis][0]:
                    observed_travel[axis] = (over, t)
            else:
                run.close(findings)

        if prev is not None and previous_t is not None:
            dt = t - previous_t
            if dt <= 0:
                continue
            dp = moves.wrap180(pitch - prev[0])
            dy = moves.wrap180(yaw - prev[1])
            vp, vy = dp / dt, dy / dt
            if abs(vp) > peak_p:
                peak_at["pitch"] = t
            if abs(vy) > peak_y:
                peak_at["yaw"] = t
            peak_p = max(peak_p, abs(vp))
            peak_y = max(peak_y, abs(vy))
            travel_p += abs(dp)
            travel_y += abs(dy)
            for axis, v in (("pitch", vp), ("yaw", vy)):
                fast = runs[("too fast", axis)]
                if abs(v) > max_dps:
                    fast.hit(t, v)
                else:
                    fast.close(findings)

        prev = (pitch, yaw)
        previous_t = t

    for run in runs.values():
        run.close(findings)

    # Aggregation must not approve a short transition whose sampled peak
    # exceeds the cap. Retain the explicit custom threshold for diagnostic
    # callers, but fail closed for normal preflight and timing proposals.
    if min_duration_s <= MIN_DURATION_S:
        for axis, peak in (("pitch", peak_p), ("yaw", peak_y)):
            if peak > max_dps and not any(f.kind == "too fast" and f.axis == axis for f in findings):
                at = peak_at[axis]
                findings.append(Finding(
                    "too fast", axis, at, at, peak, at, max_dps,
                    f"{axis} sampled peak reaches {peak:.2f} deg/s at {at:.3f}s, "
                    f"above the {max_dps:.2f} deg/s cap"))

    # A waypoint outside a measured arc is a travel fault even if it is an
    # instantaneous endpoint of a very short leg. Duration aggregation is for
    # suppressing sampled turning-point noise, not for excusing authored nodes.
    if min_duration_s <= MIN_DURATION_S:
        arrivals = move.arrival_times()
        for axis, arc in (("pitch", pitch_arc), ("yaw", yaw_arc)):
            if any(f.kind == "travel" and f.axis == axis for f in findings):
                continue
            over, at = observed_travel[axis]
            if over > TRAVEL_SLOP_DEG:
                findings.append(Finding(
                    "travel", axis, at, at, over, at, arc.usable,
                    f"{axis} path leaves its travel by {over:.1f} deg at {at:.1f}s"))
                continue
            for i, waypoint in enumerate(move.waypoints):
                over = _outside_by(arc, getattr(waypoint, axis))
                if over > TRAVEL_SLOP_DEG:
                    at = arrivals[i]
                    findings.append(Finding(
                        "travel", axis, at, at, over, at, arc.usable,
                        f"{axis} waypoint leaves its travel by {over:.1f} deg at {at:.1f}s"))
                    break

    # An axis asked to travel, whose peak demand never clears the dead band,
    # will not move at all. Judged over the whole move, not per sample: the
    # ramp through the band at each end is unavoidable and harmless.
    for axis, travel, peak in (("pitch", travel_p, peak_p),
                               ("yaw", travel_y, peak_y)):
        if travel > TRAVEL_SLOP_DEG and peak < min_dps:
            findings.append(Finding(
                kind="dead band", axis=axis, start=0.0, end=total,
                worst=peak, worst_at=0.0, limit=min_dps,
                detail=(f"{axis} never exceeds {peak:.2f} deg/s, below the "
                        f"{min_dps:.2f} dead band -- it will not move at all"),
            ))

    # Narrow Bezier peaks can fall between every fixed sample. Bound authored
    # profiles analytically; linked profiles use the conservative product bound.
    # This is a planning cap, not a claim about measured motor acceleration.
    cursor = move.waypoints[0].dwell if move.waypoints else 0.0
    for i, (start, end) in enumerate(move.legs):
        if end.has_axis_curves:
            bounds = move.axis_rate_bounds(i)
            for axis, bound in zip(("pitch", "yaw"), bounds):
                delta = moves.arc_delta(move._arc(axis), getattr(start, axis), getattr(end, axis))
                required = abs(delta) * bound / end.duration
                if required > max_dps * (1 + 1e-9):
                    findings.append(Finding(
                        "too fast", axis, cursor, cursor+end.duration,
                        required, cursor, max_dps,
                        f"{axis} curve speed bound is {required:.2f} deg/s, above "
                        f"the {max_dps:.2f} cap; lengthen this transition. "
                        "Linked-axis bounds are conservative."))
        cursor += end.duration + end.dwell

    # Worst first, and travel before speed: an axis on a stop is a ruined take,
    # a slightly late one is a fixable take.
    order = {"assessment": 0, "travel": 1, "too fast": 2, "dead band": 3}
    findings.sort(key=lambda f: (order.get(f.kind, 9), -abs(f.worst)))

    return Report(ok=not findings, findings=findings, duration=total,
                  peak_pitch_dps=peak_p, peak_yaw_dps=peak_y, samples=samples)
