"""Can this rig actually shoot this move?

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
camera in its case.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import limits, moves, response

# 50 Hz. The stick runs at 25 Hz, so sampling twice that finds anything the
# rig could actually be commanded to do without inventing detail between
# commands it will never receive.
SAMPLE_HZ = 50.0

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

    kind: str            # "travel" | "too fast" | "dead band"
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
                    f"/ {self.peak_yaw_dps:.1f} deg/s -- within the rig")
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


def check(move: moves.Move,
          pitch_arc: limits.Arc = limits.PITCH_ARC,
          yaw_arc: limits.Arc = limits.YAW_ARC,
          max_dps: float = response.MAX_DPS,
          min_dps: float = response.MIN_DPS,
          sample_hz: float = SAMPLE_HZ,
          min_duration_s: float = MIN_DURATION_S) -> Report:
    """Sample the whole move and report what the rig cannot do.

    `max_dps` defaults to the head's measured full-throw rate. Pass a preset
    cap from `response.SPEED_CAPS` to ask the narrower question: can it shoot
    this move at the sensitivity the operator has selected?

    `min_duration_s` is how long a violation has to last to be worth
    reporting. A parameter rather than a constant so the threshold itself can
    be tested: as a module constant nothing exercised it, and setting it to
    zero broke no test.
    """
    total = move.total_duration
    if len(move.waypoints) < 2 or total <= 0:
        return Report(ok=True, findings=[], duration=0.0,
                      peak_pitch_dps=0.0, peak_yaw_dps=0.0, samples=0)

    step = 1.0 / sample_hz
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
    samples = 0
    prev: tuple[float, float] | None = None
    t = 0.0
    while t <= total + 1e-9:
        point = move.sample(t)
        if point is None:
            break
        samples += 1
        pitch, yaw = point

        for axis, arc, angle in (("pitch", pitch_arc, pitch),
                                 ("yaw", yaw_arc, yaw)):
            over = _outside_by(arc, angle)
            run = runs[("travel", axis)]
            if over > TRAVEL_SLOP_DEG:
                run.hit(t, over)
            else:
                run.close(findings)

        if prev is not None:
            dp = moves.wrap180(pitch - prev[0])
            dy = moves.wrap180(yaw - prev[1])
            vp, vy = dp / step, dy / step
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
        t += step

    for run in runs.values():
        run.close(findings)

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

    # Worst first, and travel before speed: an axis on a stop is a ruined take,
    # a slightly late one is a fixable take.
    order = {"travel": 0, "too fast": 1, "dead band": 2}
    findings.sort(key=lambda f: (order.get(f.kind, 9), -abs(f.worst)))

    return Report(ok=not findings, findings=findings, duration=total,
                  peak_pitch_dps=peak_p, peak_yaw_dps=peak_y, samples=samples)
