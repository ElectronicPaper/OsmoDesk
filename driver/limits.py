"""Per-axis travel-limit awareness.

The old model was a single scalar for the whole rig:

    edge = min(pitch_up_headroom, pitch_down_headroom)
    warn = 0.25 * total_span

Two things were wrong with it. It warned on a *fraction of total travel*, which
on the pitch arc is 38 degrees -- so a head sitting at rest already glowed. And
it collapsed both axes and both directions into one number, so the UI could
only colour the whole control and never say which way was running out.

Level is not the middle of travel. Measured on an Osmo Pocket 4 Pro, resting
pitch sits 128 degrees from one stop and 30 from the other, because the head
tilts far further down and back than it does up. Any warning band expressed as
a share of total travel is therefore permanently lit at rest.

What an operator needs to know is not "am I near a stop" but "am I about to hit
one". Those differ: parked 30 degrees from a stop is fine, sailing toward it at
53 deg/s is not. So the warning is primarily *time* to impact, with a small
fixed distance floor so a head parked hard against a stop still reads as such.

Approach speed is the faster of what telemetry measures and what the stick has
just been told to do. Measurement alone lags: the rate estimate needs a quarter
second to settle, and at the measured yaw slew that is thirteen degrees of
travel already spent. Intent alone is not enough either, because a head still
carrying momentum after the stick is released is very much still approaching.

The slew constants here are measured per axis rather than taken from the stick
module nominal of 18 deg/s, which is close for pitch and a third of the truth
for yaw. A warning computed from a wrong rate model is worse than none.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from . import moves
from .moves import wrap180

# Warn once a stop is less than this far ahead in time. A second: long enough
# to come off the stick before the head arrives, short enough that ordinary
# framing moves never trip it. Wider than this and a fast yaw lights the
# indicator most of the way across its travel -- at the measured 53 deg/s,
# 1.2 s is 64 degrees, which is not a warning, it is wallpaper.
WARN_SECONDS = 1.0

# ...and always within this many degrees of a stop, however slowly you got
# there, so a parked head still shows where it is standing. Per axis, because
# yaw slews more than twice as fast and needs more room to mean the same thing.
STATIC_MARGIN_DEG = 8.0
YAW_STATIC_MARGIN_DEG = 12.0

# A head parked near a stop gets a quiet notch, never the full alarm. Resting
# yaw really does sit a few degrees off its stop, so without this cap the panel
# would light up every time the rig was simply put down -- the same nuisance
# this module exists to remove, arriving by a different route. Only an actual
# approach earns the top of the scale.
STATIC_MAX = 0.35

# ...but actually being AT the stop is not a hint, it is a fact, and it reads
# as full. The capped notch is for "parked near"; this is for "the head will
# not go that way any more", which the operator needs to recognise instantly
# rather than decode from a partly filled arc.
AT_STOP_DEG = 1.5

# Below this the axis counts as stationary. Telemetry noise on a still head is
# a few tenths of a degree per second; treating that as approach would make the
# indicator flicker at rest, which is the false alarm being fixed here.
STILL_DPS = 1.5

# How far outside its own arc an axis may read before the arc is disbelieved.
# The soft-stop margin is only 3 degrees, so without slack, driving gently into
# a stop puts the head "outside" its own travel and blanks the indicator at
# precisely the moment it is describing something real. Genuinely wrong
# calibration -- a yaw arc measured before the rig was turned around -- is out
# by tens of degrees, so this separates the two cases cleanly.
CALIBRATION_SLACK_DEG = 20.0

# Rate is smoothed over roughly this long. Shorter chatters on noise, longer
# lags the move that needs the warning.
RATE_TAU_S = 0.25

# Full-deflection slew, measured by timing a drive between the stops:
#   pitch  158.5 deg in 10.4 s at 0.65 deflection  ->  ~23 deg/s
#   yaw    159.5 deg in  3.0 s at 1.00 deflection  ->  ~53 deg/s
# The 18 deg/s the stick module assumes is close for pitch and a third of the
# truth for yaw, which is why these live here rather than being derived from it.
PITCH_MAX_DPS = 23.0
YAW_MAX_DPS = 53.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


@dataclass(frozen=True)
class Arc:
    """A travel range that may cross the +/-180 wrap.

    Stored as a start angle and the sweep from it in the increasing direction,
    because a min/max pair cannot express an arc that wraps: it rejects every
    legal position past the wrap and accepts the whole forbidden remainder on
    the other side.
    """

    start: float
    span: float
    margin: float = 3.0

    @property
    def usable(self) -> float:
        return max(0.0, self.span - 2 * self.margin)

    @property
    def low(self) -> float:
        return wrap180(self.start + self.margin)

    @property
    def high(self) -> float:
        return wrap180(self.start + self.span - self.margin)

    def offset(self, angle: float) -> float:
        """How far along the usable arc `angle` sits, in 0..360.

        Anything above `usable` is outside the arc.
        """
        return (angle - self.low) % 360.0

    def contains(self, angle: float) -> bool:
        return self.offset(angle) <= self.usable

    def plausible(self, angle: float) -> bool:
        """Inside the arc, or close enough that the arc is still believable.

        Sitting in the soft-stop margin is normal operation, not evidence that
        the calibration is wrong.
        """
        off = self.offset(angle)
        return off <= self.usable + CALIBRATION_SLACK_DEG or             off >= 360.0 - CALIBRATION_SLACK_DEG

    def headroom(self, angle: float) -> tuple[float, float]:
        """Degrees left toward the low end and the high end, in that order.

        Clamped rather than zeroed: a head parked just past the usable end has
        no room that way and all of it the other way, and saying "no room in
        either direction" would claim the head cannot move at all.
        """
        off = self.offset(angle)
        if off > self.usable:
            past_high = off - self.usable
            past_low = 360.0 - off
            return (0.0, self.usable) if past_low <= past_high else (self.usable, 0.0)
        return (off, self.usable - off)


@dataclass
class AxisLimit:
    """Tracks one axis and reports how close each end is, 0..100 per end.

    `calibrated` exists because the yaw arc was measured with the body held
    still. Pick the rig up, turn around, and the stored yaw arc no longer
    describes the hardware. Rather than show a confident wrong number, an axis
    reading outside its own arc is marked uncalibrated and the UI draws nothing
    for it.
    """

    arc: Arc
    name: str = ""
    calibrated: bool = True
    static_margin: float = STATIC_MARGIN_DEG
    max_dps: float = PITCH_MAX_DPS

    _angle: float | None = field(default=None, repr=False)
    _rate: float = field(default=0.0, repr=False)
    _at: float = field(default=0.0, repr=False)
    _commanded: float = field(default=0.0, repr=False)

    def reset(self) -> None:
        self._angle = None
        self._rate = 0.0
        self._commanded = 0.0
        self.calibrated = True

    def command(self, deflection: float) -> None:
        """Stick deflection, -1..1, positive toward the arc high end.

        Intent counts as well as motion. A stationary head that has just been
        given a full-deflection command is about to be moving fast, and waiting
        for the measured rate to catch up spends the whole warning window: the
        rate estimate needs a quarter second to settle, which at 53 deg/s is
        thirteen degrees of travel already spent.
        """
        self._commanded = max(-1.0, min(1.0, deflection)) * self.max_dps

    def feed(self, angle: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._angle is None:
            self._angle, self._at = angle, now
            self.calibrated = self.arc.plausible(angle)
            return
        dt = now - self._at
        if dt <= 0:
            return
        # Shortest-way delta, so crossing the wrap does not read as a 359 deg
        # lurch and momentarily peg the warning.
        inst = wrap180(angle - self._angle) / dt
        alpha = 1.0 - math.exp(-dt / RATE_TAU_S)
        self._rate += (inst - self._rate) * alpha
        self._angle, self._at = angle, now
        if not self.arc.plausible(angle):
            self.calibrated = False

    @property
    def rate(self) -> float:
        """Signed degrees per second, positive toward the high end of the arc."""
        return self._rate

    @property
    def angle(self) -> float | None:
        return self._angle

    def _approach(self, remaining: float, closing: float) -> float:
        """0..1 for one end. `closing` is deg/s toward it, negative if away."""
        if remaining <= AT_STOP_DEG:
            return 1.0
        static = STATIC_MAX * _clamp01(
            (self.static_margin - remaining) / self.static_margin)
        timed = 0.0
        if closing > STILL_DPS:
            timed = _clamp01((WARN_SECONDS - remaining / closing) / WARN_SECONDS)
        return max(static, timed)

    def _closing(self, toward_high: bool) -> float:
        """Fastest credible approach to one end: measured or commanded."""
        sign = 1.0 if toward_high else -1.0
        return max(sign * self._rate, sign * self._commanded)

    def report(self) -> dict:
        """What the UI draws. `low` and `high` are 0..100 for each end."""
        if self._angle is None or not self.calibrated:
            return {"known": False, "low": 0, "high": 0,
                    "low_deg": None, "high_deg": None, "rate": 0.0,
                    "span": round(self.arc.usable, 1), "at": None}
        to_low, to_high = self.arc.headroom(self._angle)
        return {
            "known": True,
            "low": int(round(100 * self._approach(to_low, self._closing(False)))),
            "high": int(round(100 * self._approach(to_high, self._closing(True)))),
            "low_deg": round(to_low, 1),
            "high_deg": round(to_high, 1),
            "rate": round(self._rate, 1),
            "span": round(self.arc.usable, 1),
            "at": round(self._angle, 1),
        }


# Derived from the measured arcs in driver/moves.py rather than restated here.
# They were written out twice, which is two places to forget after the head is
# serviced -- and the path planner and the limit monitor disagreeing about
# where the stops are is the worst of all the ways this can go wrong.
#
# The direction matters as much as the endpoints. Travel runs from the START in
# the INCREASING direction: -48.1 + 273.2 wraps to -134.9, the other measured
# stop. Naming the stops the other way round puts the far end at 138.3, which
# is not a stop at all, and every reported headroom is then wrong by ninety
# degrees while still looking entirely plausible.
#
# Re-measure with tools/sweep_travel.py if the head is serviced, and change the
# numbers in moves.py -- these follow.
def _from(limits: moves.SoftLimits) -> Arc:
    return Arc(start=limits.arc_from, span=limits.arc_span,
               margin=limits.margin)


PITCH_ARC = _from(moves.PITCH_LIMITS)
YAW_ARC = _from(moves.YAW_LIMITS)


class LimitMonitor:
    """Both axes together, fed from gimbal telemetry."""

    def __init__(self, pitch: Arc = PITCH_ARC, yaw: Arc = YAW_ARC):
        self.pitch = AxisLimit(pitch, "pitch",
                               static_margin=STATIC_MARGIN_DEG,
                               max_dps=PITCH_MAX_DPS)
        self.yaw = AxisLimit(yaw, "yaw",
                             static_margin=YAW_STATIC_MARGIN_DEG,
                             max_dps=YAW_MAX_DPS)

    def feed(self, pitch: float, yaw: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.pitch.feed(pitch, now)
        self.yaw.feed(yaw, now)

    def command(self, tilt: float, pan: float) -> None:
        """Current stick deflection, so intent feeds the warning as well."""
        self.pitch.command(tilt)
        self.yaw.command(pan)

    def reset(self) -> None:
        self.pitch.reset()
        self.yaw.reset()

    @property
    def worst(self) -> int:
        """One 0..100 for consumers that can only show a single number.

        Deliberately narrow in scope: the Core2 haptics need one figure to pick
        a pulse strength. Nothing visual should use it -- showing one number
        for four independent ends is what made the old indicator light the
        whole control because of one stop on one axis.
        """
        vals: list[int] = []
        for ax in (self.pitch, self.yaw):
            r = ax.report()
            if r["known"]:
                vals += [r["low"], r["high"]]
        return max(vals) if vals else 0

    def report(self) -> dict:
        return {"pitch": self.pitch.report(),
                "yaw": self.yaw.report(),
                "worst": self.worst}
