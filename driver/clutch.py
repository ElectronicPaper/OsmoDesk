"""Kinetic takeover: point the controller, the camera follows -- from where it is.

The naive mapping of controller attitude to camera attitude is unusable on set.
The instant you engage, the frame snaps to wherever you happen to be holding
the box. You cannot pick up control mid-shot, cannot re-grip, cannot hand it to
someone else.

A clutch fixes it. On engage, both attitudes are sampled and the *difference*
is stored, so the first commanded target equals the current position exactly:

    target = camera_at_engage + (controller_now - controller_at_engage) * gain

Ratcheting falls out for free: release, rotate your body back to something
comfortable, re-engage, carry on. With ~158 deg of pitch and ~86 deg of yaw to
work with, that is not a luxury.

Design rules here came from a camera operator's review and are deliberate:

* **Momentary, never latching.** Hold to control, release to relinquish. A
  controller put down on an apple box must never still own the head.
* **0.5x default gain.** Hand rotation is coarse; halving it is the difference
  between a usable move and a wobble. Fixed presets only -- an operator can
  learn a stable response curve, but not one that silently changes leverage.
* **Ratcheting restores wrist comfort, not gimbal travel.** The head still has
  the same hard limits; `headroom` is what the UI should show.
* **Release eases to zero rather than stopping dead**, but a fault zeroes
  immediately -- easing a fault is how you keep moving into a stop.
* **Yaw comes from the gyro rate, integrated only while engaged.** The
  MPU6886 has no magnetometer, so its absolute yaw drifts; pitch is
  gravity-referenced and does not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .moves import SoftLimits, wrap180

# Fixed, nameable sensitivities. No adaptive gain: a controller that changes
# leverage on its own cannot be learned.
GAINS = {"fine": 0.25, "normal": 0.5, "fast": 1.0}
DEFAULT_GAIN = "normal"

# Release is a gentle hand-off, not a stop. A fault is a stop.
RELEASE_RAMP_S = 0.12

# Taper commands over the last tenth of usable travel so the head eases into
# its limit instead of arriving at it. Motion away from the stop is never
# restricted -- deadlocking the operator at a clamp is worse than the clamp.
SOFT_STOP_FRACTION = 0.10


@dataclass
class ClutchState:
    engaged: bool = False
    releasing: bool = False
    engaged_at: float = 0.0
    imu_pitch_ref: float = 0.0
    gimbal_pitch_ref: float = 0.0
    gimbal_yaw_ref: float = 0.0
    grabs: int = 0
    near_limit: bool = False
    lock_tilt: bool = False
    lock_pan: bool = False

    def to_dict(self) -> dict:
        return {
            "engaged": self.engaged,
            "releasing": self.releasing,
            "grabs": self.grabs,
            "near_limit": self.near_limit,
            "lock_tilt": self.lock_tilt,
            "lock_pan": self.lock_pan,
            "held_for": round(time.monotonic() - self.engaged_at, 1) if self.engaged else 0.0,
        }


class Clutch:
    def __init__(self, gain: str = DEFAULT_GAIN,
                 limits: SoftLimits | None = None):
        self.set_gain(gain)
        self.limits = limits or SoftLimits()
        self.state = ClutchState()
        self._yaw_accum = 0.0
        self._last_rate_at: float | None = None
        self._release_at: float | None = None
        self._last_target: tuple[float, float] | None = None

    # -- configuration ------------------------------------------------------

    def set_locks(self, tilt: bool | None = None, pan: bool | None = None) -> None:
        """Freeze an axis.

        A locked axis holds the angle it had when the lock went on, so a pure
        pan stays level instead of wandering in tilt while the wrist rolls.
        This is the single most useful constraint on a handheld kinetic
        controller: hands are not good at moving one axis at a time.
        """
        if tilt is not None:
            self.state.lock_tilt = tilt
        if pan is not None:
            self.state.lock_pan = pan

    def set_gain(self, gain: str) -> None:
        if gain not in GAINS:
            raise ValueError(f"unknown gain {gain!r}; expected one of {sorted(GAINS)}")
        self.gain_name = gain
        self.gain = GAINS[gain]
        self.tilt_gain_name = gain
        self.pan_gain_name = gain
        self.tilt_gain = self.gain
        self.pan_gain = self.gain

    def set_axis_response(self, tilt: str | None = None,
                          pan: str | None = None) -> None:
        """Set learned leverage independently for tilt and pan.

        Validate the complete request before changing either axis. A malformed
        serial action must not leave the controller half-updated. The legacy
        global gain remains a compatibility surface; when both axes meet, it
        follows them again so older clients report an honest common value.
        """
        requested = {"tilt": tilt, "pan": pan}
        for axis, name in requested.items():
            if name is not None and name not in GAINS:
                raise ValueError(
                    f"unknown {axis} response {name!r}; "
                    f"expected one of {sorted(GAINS)}"
                )
        if tilt is not None:
            self.tilt_gain_name = tilt
            self.tilt_gain = GAINS[tilt]
        if pan is not None:
            self.pan_gain_name = pan
            self.pan_gain = GAINS[pan]
        if self.tilt_gain_name == self.pan_gain_name:
            self.gain_name = self.tilt_gain_name
            self.gain = self.tilt_gain

    # -- engagement ---------------------------------------------------------

    def engage(self, imu_pitch: float, gimbal_pitch: float, gimbal_yaw: float) -> None:
        """Grab from the current position. The frame must not move.

        Sampling both references at this instant is the whole trick: the offset
        between hand and camera is frozen here, so the first target equals the
        current position exactly.
        """
        s = self.state
        s.engaged = True
        s.releasing = False
        s.engaged_at = time.monotonic()
        s.imu_pitch_ref = imu_pitch
        s.gimbal_pitch_ref = gimbal_pitch
        s.gimbal_yaw_ref = gimbal_yaw
        s.grabs += 1
        s.near_limit = False
        # Locks persist across a re-grab (the operator set them deliberately),
        # but the frozen reference becomes wherever the camera is now.
        self._yaw_accum = 0.0
        self._last_rate_at = None
        self._release_at = None
        self._last_target = (gimbal_pitch, gimbal_yaw)

    def release(self) -> None:
        """Let go gently. The camera holds the frame it was last given."""
        if not self.state.engaged:
            return
        self.state.engaged = False
        self.state.releasing = True
        self._release_at = time.monotonic()

    def abort(self) -> None:
        """Fault or STOP: drop control immediately, no easing."""
        self.state.engaged = False
        self.state.releasing = False
        self.state.near_limit = False
        self._release_at = None
        self._last_target = None

    def rebase(self, imu_pitch: float, gimbal_pitch: float, gimbal_yaw: float) -> None:
        """Re-zero without releasing.

        Used after a limit clamp so the controller cannot bank up an offset it
        can never spend -- otherwise the operator rotates back and the camera
        sits still until the phantom debt is repaid.
        """
        if not self.state.engaged:
            return
        grabs = self.state.grabs
        self.engage(imu_pitch, gimbal_pitch, gimbal_yaw)
        self.state.grabs = grabs        # a rebase is not a new grab

    # -- output -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.state.engaged or self.state.releasing

    def release_scale(self) -> float:
        """1.0 while held, easing to 0 across the release ramp."""
        if self.state.engaged:
            return 1.0
        if not self.state.releasing or self._release_at is None:
            return 0.0
        done = (time.monotonic() - self._release_at) / RELEASE_RAMP_S
        if done >= 1.0:
            self.state.releasing = False
            self._release_at = None
            return 0.0
        return 1.0 - done

    def feedforward(self, pitch_rate_dps: float, yaw_rate_dps: float
                    ) -> tuple[float, float]:
        """Commanded camera rate, in degrees per second, per axis.

        Proportional control only produces output once the camera has already
        fallen behind the hand, which is exactly the rubber-band feel. The
        hand's angular rate is known directly from the gyro, so command it and
        leave the P term to clean up the residual.
        """
        s = self.state
        if not s.engaged:
            return (0.0, 0.0)
        vp = 0.0 if s.lock_tilt else pitch_rate_dps * self.tilt_gain
        vy = 0.0 if s.lock_pan else yaw_rate_dps * self.pan_gain
        return (vp, vy)

    def target(self, imu_pitch: float, yaw_rate_dps: float = 0.0
               ) -> tuple[float, float] | None:
        """Where the camera should point now, or None when not in control."""
        s = self.state
        if not s.engaged:
            # While releasing, keep asking for the last target so the head
            # holds its frame instead of drifting during the ramp.
            return self._last_target if s.releasing else None

        if s.lock_tilt:
            pitch = s.gimbal_pitch_ref          # frozen where the lock caught it
        else:
            d_pitch = wrap180(imu_pitch - s.imu_pitch_ref) * self.tilt_gain
            pitch = wrap180(s.gimbal_pitch_ref + d_pitch)

        now = time.monotonic()
        if self._last_rate_at is not None and not s.lock_pan:
            # Do not integrate while locked: otherwise the wrist banks up an
            # offset that snaps the camera round the moment the lock lifts.
            self._yaw_accum += yaw_rate_dps * (now - self._last_rate_at)
        self._last_rate_at = now
        yaw = (s.gimbal_yaw_ref if s.lock_pan
               else wrap180(s.gimbal_yaw_ref + self._yaw_accum * self.pan_gain))

        clamped = self.limits.clamp_pitch(pitch)
        s.near_limit = self.near_soft_stop(clamped)
        self._last_target = (clamped, yaw)
        return self._last_target

    # -- limits -------------------------------------------------------------

    def near_soft_stop(self, pitch: float) -> bool:
        """True in the last tenth of usable travel at either end."""
        span = self.limits.usable_span
        if span <= 0:
            return True
        offset = (pitch - self.limits.start) % 360.0
        edge = span * SOFT_STOP_FRACTION
        return offset <= edge or offset >= span - edge

    def headroom(self, pitch: float) -> tuple[float, float]:
        """Degrees of travel left toward each end of the arc.

        The UI should show this rather than a raw angle: pitch crosses the
        +/-180 wrap, so a bare Euler readout jumps from +179 to -179 and reads
        as a discontinuity that is not there.
        """
        span = self.limits.usable_span
        offset = (pitch - self.limits.start) % 360.0
        if offset > span:                       # outside the arc entirely
            return (0.0, 0.0)
        return (round(offset, 1), round(span - offset, 1))
