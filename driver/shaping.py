"""Ease-in and ease-out for live control.

Programmed moves have had easing from the start, because a timed path can be
shaped in advance. Live control could not: whatever the stick said arrived at
the head immediately, so every start was a snap and every release was a dead
stop. On camera that reads as a jolt at both ends of every move, and it is the
single thing that most separates a rig that looks operated from one that looks
motorised.

The whole design turns on one asymmetry: **almost all the shaping lives in the
release, and almost none in the attack.**

That is not a compromise, it is the resolution of the central conflict. A
filter slow enough to make a beautiful stop also delays the start, and a hand
controller that lags the hand feels broken -- which is precisely the complaint
that made kinetic control unusable on this rig before. But the disconnection an
operator feels is about *onset*, not about the tail. Once the gesture is over, a
decaying tail does not read as lag; it reads as inertia, the way a well-dragged
fluid head finishes a move on its own. So the attack is near-direct and every
preset spends its character on the settle.

Both paths use the same machinery -- an online jerk-limited velocity governor
-- with different budgets. Every tick it re-plans toward the newest requested
velocity while bounding both the acceleration and the rate of change of
acceleration.

Three shapes were considered and two rejected:

* A plain slew-rate limiter bounds acceleration, which is what a hard travel
  stop needs, but its acceleration switches instantly between zero and the
  limit. That step is visible at both ends of a slow move: a kick as the ramp
  starts and a corner where velocity reaches zero. It looks servo-driven.
* A critically damped second-order filter (or two one-poles in series) has a
  lovely bell-shaped decay and no corner at all. It was very nearly the choice
  here. It was rejected because it gives no hard bound on stopping distance,
  its tail is asymptotic rather than finite, and its effective damping shifts
  once saturation and 40 ms sampling are involved. On a head with hard
  mechanical stops, "usually stops in about this far" is not good enough.
* A jerk-limited profile gives the smooth result for the same reason the
  second-order filter does -- acceleration arrives and departs progressively,
  so there is no hitch at release and no corner at zero -- while still
  bounding both acceleration and stopping distance. It costs more state and
  needs care around reversals. That is the one implemented.

The limits are on the *commanded* profile. With no acceleration feedback they
guarantee the smoothness of what is sent, not of what the head physically
does; the numbers are starting points to be trimmed against footage.

Everything works in the linearised degrees-per-second domain, so a preset means
the same thing regardless of the camera's own lumpy response curve.

Three things bypass the shaping completely, and they are listed here because
getting this wrong is how someone gets hurt: an emergency stop, a fault, and a
control link that has gone quiet. `abort()` zeroes the state instantly and is
what all of those must call. A tail is a nicety, and none of those is a moment
for one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Below this the head cannot hold a speed anyway (measured), so a tail that
# trails off into micro-velocities is a creep that never quite arrives.
STOP_BELOW_DPS = 0.35

# However cinematic the preset, the head must be still within this long after
# the operator lets go. A tail is a look; an indefinite one is a runaway.
MAX_TAIL_S = 1.5


@dataclass(frozen=True)
class Ramp:
    """One named feel.

    Four numbers, because starting and stopping are separate decisions:
    `accel_in`/`jerk_in` govern gaining speed and `accel_out`/`jerk_out`
    govern coming to rest, in deg/s^2 and deg/s^3.
    """

    name: str
    accel_in: float
    jerk_in: float
    accel_out: float
    jerk_out: float
    blurb: str
    speed_cap: float | None = None

    def stop_distance(self, v: float) -> float:
        """Degrees still travelled bringing `v` to rest under these limits.

        Two contributions: the constant-acceleration part, and the extra
        covered while the acceleration itself ramps up and back down. Ignoring
        the second underestimates the room needed, which at a hard stop is the
        error that costs a gimbal.
        """
        v = abs(v)
        if self.accel_out <= 0:
            return 0.0
        d = v * v / (2.0 * self.accel_out)
        if self.jerk_out > 0:
            d += v * self.accel_out / (2.0 * self.jerk_out)
        return d

    @property
    def stop_time(self) -> float:
        """Roughly how long a full-speed stop takes, for the UI to quote."""
        if self.accel_out <= 0:
            return 0.0
        t = 42.0 / self.accel_out
        if self.jerk_out > 0:
            t += self.accel_out / self.jerk_out
        return t


# Named for the shot, not the numbers. An operator can learn "glide is the one
# for a reveal"; nobody learns "settle equals six hundred milliseconds".
RAMPS: dict[str, Ramp] = {
    "direct": Ramp("direct", 1e6, 1e9, 1e6, 1e9,
                   "raw. Nothing added -- whip pans, rigging, and for trusting "
                   "the rest of the presets against"),
    "news": Ramp("news", 360.0, 9000.0, 220.0, 5500.0,
                 "crisp. Following a subject that moves without warning. At "
                 "this speed it is really a rounded slew, not a full S-curve"),
    "fluid": Ramp("fluid", 240.0, 3000.0, 120.0, 1200.0,
                  "the default. Like a well-dragged fluid head"),
    "glide": Ramp("glide", 200.0, 1800.0, 90.0, 700.0,
                  "reveals, product, landscape. The move lands itself"),
    "float": Ramp("float", 160.0, 1000.0, 70.0, 350.0,
                  "the most cinematic and the least responsive. Locked-off "
                  "drifts, not following anything"),
    "long lens": Ramp("long lens", 150.0, 900.0, 70.0, 350.0,
                      "telephoto, where any hitch is magnified. Also caps the "
                      "top speed", speed_cap=12.0),
}

DEFAULT_RAMP = "fluid"

# One trim, not a curve editor. If a preset is nearly right, this is the only
# adjustment; if it is not, the answer is a different preset.
SETTLE_TRIMS = {"short": 0.5, "normal": 1.0, "long": 2.0}

# Independent axis feel, layered over the chosen whole-shot ramp. Lowering
# the factor gives a heavier, quieter axis; raising it makes that axis acquire
# intent sooner. Balanced is exactly the historical shaper behavior.
STABILITY_FACTORS = {"quiet": 0.55, "balanced": 1.0, "responsive": 1.6}


def get_ramp(name: str) -> Ramp:
    try:
        return RAMPS[name]
    except KeyError:
        raise ValueError(f"unknown ramp: {name!r}") from None


class AxisShaper:
    """Jerk-limited velocity governor for one axis.

    Stateful; call `update` at a fixed rate. It re-plans every tick toward the
    newest target rather than committing to a profile, so a hand that changes
    its mind mid-move is followed instead of ignored.
    """

    def __init__(self, ramp: Ramp | str = DEFAULT_RAMP, trim: float = 1.0):
        self.ramp = ramp if isinstance(ramp, Ramp) else get_ramp(ramp)
        self.trim = trim
        self.stability_factor = 1.0
        self.velocity = 0.0
        self.accel = 0.0
        self._tail_for = 0.0

    # -- configuration -------------------------------------------------------

    def set_ramp(self, ramp: Ramp | str) -> None:
        self.ramp = ramp if isinstance(ramp, Ramp) else get_ramp(ramp)

    def set_trim(self, trim: float) -> None:
        """One knob, not a curve editor: stretch or shorten the settle.

        If a preset is nearly right this is the only adjustment. If it is not,
        the answer is a different preset.
        """
        self.trim = max(0.25, min(4.0, trim))

    def set_stability(self, factor: float) -> None:
        """Scale acceleration and jerk without changing the speed ceiling."""
        self.stability_factor = max(0.25, min(2.0, float(factor)))

    # -- state ---------------------------------------------------------------

    def abort(self) -> None:
        """Stop, meaning it: no tail, no residual acceleration.

        Everything dangerous routes here -- emergency stop, fault, a control
        link that stopped talking, telemetry that stopped making sense.
        Clearing the stored acceleration matters as much as zeroing the
        velocity: left behind, it would resume the move the instant the
        operator re-engaged, after they had already stopped the rig.
        """
        self.velocity = 0.0
        self.accel = 0.0
        self._tail_for = 0.0

    @property
    def moving(self) -> bool:
        return self.velocity != 0.0

    def coast_distance(self) -> float:
        """Degrees still to travel if the operator let go right now."""
        return self._out_ramp().stop_distance(self.velocity)

    def _out_ramp(self) -> Ramp:
        """The release budget with the trim applied.

        A longer settle is a gentler deceleration, so the trim divides.
        """
        r = self.ramp
        factor = self.stability_factor
        return Ramp(r.name, r.accel_in * factor, r.jerk_in * factor,
                    r.accel_out * factor / self.trim,
                    r.jerk_out * factor / self.trim,
                    r.blurb, r.speed_cap)

    # -- the loop ------------------------------------------------------------

    def update(self, target: float, dt: float) -> float:
        if dt <= 0:
            return self.velocity
        cap = self.ramp.speed_cap
        if cap is not None:
            target = max(-cap, min(cap, target))

        # Which budget applies depends on whether the head is being asked to
        # gain speed or shed it -- not on the sign of the error. Crossing zero
        # counts as shedding: it has to come to rest before it can leave the
        # other way.
        # A reversal counts as gaining speed, not shedding it. Physically the
        # head must still pass through zero, but the operator has actively
        # asked for the other direction -- that is intent, not a release, and
        # making them wait out a glide-rate deceleration first would feel
        # broken. The jerk limit keeps the turnaround smooth regardless.
        reversing = target * self.velocity < 0
        speeding_up = reversing or abs(target) > abs(self.velocity)
        if speeding_up:
            a_lim = self.ramp.accel_in * self.stability_factor
            j_lim = self.ramp.jerk_in * self.stability_factor
            self._tail_for = 0.0
        else:
            out = self._out_ramp()
            a_lim, j_lim = out.accel_out, out.jerk_out
            if target == 0.0:
                self._tail_for += dt

        err = target - self.velocity
        # How much more velocity is spent just bleeding the current
        # acceleration back to zero. Steering on the raw error instead makes
        # the governor sail past the target and hunt around it.
        bleed = self.accel * abs(self.accel) / (2.0 * j_lim) if j_lim > 0 else 0.0
        wanted = a_lim if err - bleed > 0 else (-a_lim if err - bleed < 0 else 0.0)

        step = j_lim * dt
        self.accel += max(-step, min(step, wanted - self.accel))

        before = self.velocity
        self.velocity += self.accel * dt

        if (target - before) * (target - self.velocity) < 0:
            self.velocity = target
            self.accel = 0.0

        # However pretty the preset, the head is still within a second and a
        # half of the operator letting go. A tail is a look; an unbounded one
        # is a runaway.
        if target == 0.0 and self._tail_for > MAX_TAIL_S:
            self.velocity = 0.0
            self.accel = 0.0

        # Below the slowest speed the head can hold, a decaying command is a
        # creep that never arrives -- and on this hardware it can chatter at
        # breakaway instead of moving at all.
        if target == 0.0 and abs(self.velocity) < STOP_BELOW_DPS:
            self.velocity = 0.0
            self.accel = 0.0
            self._tail_for = 0.0
        return self.velocity


class MotionShaper:
    """Both axes, sharing a preset."""

    def __init__(self, ramp: Ramp | str = DEFAULT_RAMP, trim: float = 1.0):
        self.pitch = AxisShaper(ramp, trim)
        self.yaw = AxisShaper(ramp, trim)

    @property
    def ramp_name(self) -> str:
        return self.pitch.ramp.name

    def set_ramp(self, ramp: Ramp | str) -> None:
        self.pitch.set_ramp(ramp)
        self.yaw.set_ramp(ramp)

    def set_trim(self, trim: float) -> None:
        self.pitch.set_trim(trim)
        self.yaw.set_trim(trim)

    def set_axis_stability(self, pitch: float, yaw: float) -> None:
        self.pitch.set_stability(pitch)
        self.yaw.set_stability(yaw)

    def abort(self) -> None:
        self.pitch.abort()
        self.yaw.abort()

    @property
    def moving(self) -> bool:
        return self.pitch.moving or self.yaw.moving

    def update(self, pitch_dps: float, yaw_dps: float,
               dt: float) -> tuple[float, float]:
        return (self.pitch.update(pitch_dps, dt),
                self.yaw.update(yaw_dps, dt))


# Worst case age of the pose the limit was computed from, plus the command
# period it will be acted on in. At full speed this is several degrees of
# travel that has already happened by the time anything reacts to it.
LATENCY_S = 0.09

# The head keeps moving after the command reaches zero, and this is nothing to
# do with the shaping -- it is the camera's own ramp-down, which no open-loop
# profile can shorten.
#
# Measured, five runs per preset from the 42 deg/s cap: `direct`, which
# commands an instant stop, still coasted a median 17.2 deg, and `float`
# coasted 36.4 deg against a commanded profile worth 16.8. Dividing the
# unexplained remainder by the release speed gives 0.41 s and 0.47 s -- near
# enough the same figure from two very different presets, which is what a
# fixed time constant looks like.
#
# Leaving it out is not a cosmetic error. brake_limit was reserving only the
# commanded profile, so it under-reserved by 13 to 16 deg at full speed and
# would have let the head run into a hard stop while believing it had room.
# Rounded up from the slower of the two measurements for margin.
HARDWARE_COAST_S = 0.50


def brake_limit(remaining_deg: float, ramp: Ramp | str,
                margin: float = 2.0) -> float:
    """Fastest speed from which the head still stops short of a travel stop.

    Applied *after* the governor, never through it: a soft landing is only soft
    if the deceleration starts far enough out, and the tail must never be able
    to carry the head into a hard stop.

    The budget has three parts, and the third was missing for a while.
    Telemetry is up to ~50 ms old and the next command lands up to one 40 ms
    period later; the commanded profile then takes its own distance to wind
    down; and finally the head coasts on regardless, because the camera runs
    its own deceleration that no open-loop command can shorten. Measuring only
    the first two under-reserves by more than a dozen degrees at speed, which
    is a limit that reports itself safe and then arrives with a bump.
    """
    r = ramp if isinstance(ramp, Ramp) else get_ramp(ramp)
    usable = max(0.0, remaining_deg - margin)
    if usable <= 0:
        return 0.0
    if r.accel_out <= 0:
        return float("inf")
    # Solve for the largest v whose stopping distance plus latency travel fits.
    lo, hi = 0.0, 200.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        needed = (r.stop_distance(mid)
                  + mid * LATENCY_S
                  + mid * HARDWARE_COAST_S)
        if needed <= usable:
            lo = mid
        else:
            hi = mid
    return lo
