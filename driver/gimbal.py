"""Gimbal control: stick encoding, and an absolute-attitude controller built
on top of it.

Why a controller instead of an absolute-angle command: the only gimbal motion
opcode confirmed on Pocket 4 / 4 Pro is the stick, `0x04/0x01`, which is a
velocity-style input. The absolute-angle opcodes (`0x04/0x0A`, `0x04/0x14`)
come from Pocket 3 reverse engineering and were never observed to be honoured.
So absolute IMU mirroring is done as a proportional controller: measure the
error between the IMU attitude and the gimbal's reported attitude, and deflect
the stick in proportion.
"""

from __future__ import annotations

import logging
import threading
import time

from . import commands, moves, response, shaping
from .datalink import Datalink

log = logging.getLogger(__name__)

# Stick range from the labelled capture.
CENTER = 1024
TRAVEL = 550
AXIS_MIN = CENTER - TRAVEL  # 474
AXIS_MAX = CENTER + TRAVEL  # 1574

# Rest snap. Inside this, both axes stay at centre.
DEADZONE = 0.08

# Mimo streams 0x04/0x01 only while the on-screen stick is held, at 25 Hz.
STICK_HZ = 25
STICK_INTERVAL_S = 1.0 / STICK_HZ

# Stop streaming after this long without a fresh input, so the gimbal parks.
INPUT_TIMEOUT_S = 0.5

# Measured on an Osmo Pocket 4 Pro with `run.py --map-axes`: a stick tilt-up
# command (axis0 above centre) DECREASES the reported pitch by ~17 deg. The
# controller has to invert accordingly or it drives away from the target and
# saturates.
TILT_SIGN = -1.0

# UNRESOLVED. Two hardware observations disagree:
#
#   A stick sweep drove yaw +0.8 -> -134.8 and -0.8 -> -48.1, implying
#   YAW_SIGN = -1 and an upper stop near -48.
#
#   A programmed move tracked yaw from -48 up to -26 with sub-degree error,
#   which is the opposite sign AND past that supposed stop.
#
# The likely explanation is that `yaw` (angle_b) and `yaw_alt` (angle_c) are
# body-frame and motor-joint angles -- --map-axes showed them moving with
# opposite signs -- and the pan envelope may also vary with pitch. Until that
# is separated, +1 is what tracked well in a real move. Pitch is fully
# characterised; yaw is not. Programme pitch-dominant moves for now.
YAW_SIGN = 1.0

# How far ahead to sample the path when estimating its velocity.
FF_LOOKAHEAD_S = 0.05


def _wrap180(delta: float) -> float:
    """Shortest signed angle difference. Pitch wraps at +/-180."""
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    return delta


def axis(normalized: float, gain: float = 1.0) -> int:
    """Map a unit axis (-1..1) onto 1024 +/- 550."""
    n = max(-1.0, min(1.0, normalized))
    if abs(n) < DEADZONE:
        return CENTER
    v = int(round(CENTER + n * TRAVEL * gain))
    return max(AXIS_MIN, min(AXIS_MAX, v))


class GimbalStick:
    """Streams 0x04/0x01 at 25 Hz while an input is held.

    `set_axes` takes unit floats: tilt +1 = up, pan +1 = right.
    """

    def __init__(self, link: Datalink, gain: float = 1.0,
                 speed_cap: float = response.SPEED_CAPS["normal"],
                 ramp: str = shaping.DEFAULT_RAMP):
        self.link = link
        self.gain = gain
        # Ease-in and ease-out for live control. Shaping happens in degrees per
        # second, before the response curve is inverted, so a preset means the
        # same thing regardless of the camera's own lumpy deflection response.
        self.shaper = shaping.MotionShaper(ramp)
        # Top speed in deg/s at full operator input. The named presets set this
        # rather than scaling a deflection, because a deflection does not mean
        # the same thing at both ends of its own range on this camera.
        self.speed_cap = speed_cap
        # Targets are now rates in the telemetry frame rather than
        # deflections, because that is the domain the shaper works in and the
        # only one where "accelerate at 250 deg/s squared" means anything.
        self._tilt = 0.0
        self._pan = 0.0
        self._last_input = 0.0
        # Whether the last thing the operator said was "stop". A deliberate
        # zero is a complete instruction: the client has nothing further to
        # send and its silence afterwards is not a fault. A non-zero command
        # that stops arriving is a fault, and the two must not be confused.
        self._last_was_stop = True
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, name="gimbal-stick", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_axes(self, tilt: float, pan: float) -> None:
        """Operator input, -1..1 per axis. Up and right are positive.

        This is an *intent*, not a deflection: it is shaped by the chosen expo
        and speed cap and then run backwards through the camera's measured
        response, so equal movements of the operator's hand produce equal
        changes in speed. Sending the raw value straight through is what made
        the stick feel dead, then grabby, then flat, then violent.
        """
        # Operator frame to telemetry frame. Up is positive for the operator
        # but *decreases* reported pitch, which is exactly what TILT_SIGN
        # encodes -- so convert through it rather than hard-coding a second
        # copy of the same fact that could later disagree with the first.
        self.set_rate(response.rate_for_input(tilt, self.speed_cap) / TILT_SIGN,
                      response.rate_for_input(pan, self.speed_cap) / YAW_SIGN)

    def wire_deflections(self, pitch_dps: float,
                         yaw_dps: float) -> tuple[float, float]:
        """The unit deflections a pair of telemetry-frame rates becomes.

        The pump uses this, and so does anything checking the operator-to-wire
        mapping, because the internal targets are rates now and reading them
        as deflections is how a silent axis inversion would get through.
        """
        return (response.deflection_for_rate(TILT_SIGN * pitch_dps),
                response.deflection_for_rate(YAW_SIGN * yaw_dps))

    def set_ramp(self, name: str) -> None:
        """Change the ease preset. Takes effect on the next control tick."""
        self.shaper.set_ramp(name)

    def set_axis_stability(self, tilt: str = "balanced",
                           pan: str = "balanced") -> None:
        """Layer independent axis weight over the shared cinematic ramp."""
        unknown = [name for name in (tilt, pan)
                   if name not in shaping.STABILITY_FACTORS]
        if unknown:
            raise ValueError(
                f"unknown axis stability {unknown[0]!r}; expected one of "
                f"{sorted(shaping.STABILITY_FACTORS)}"
            )
        self.shaper.set_axis_stability(
            shaping.STABILITY_FACTORS[tilt],
            shaping.STABILITY_FACTORS[pan],
        )

    def abort(self) -> None:
        """Stop now, with no shaped tail left to resume.

        Distinct from `release()`, which is an operator letting go and gets the
        cinematic settle. This is for an emergency stop, a fault, or telemetry
        loss -- none of which is a moment for a graceful landing.
        """
        with self._lock:
            self._tilt = self._pan = 0.0
            self._last_input = time.monotonic()
        self.shaper.abort()
        self.link.send_frame(commands.gimbal_stick(CENTER, CENTER))

    def set_rate(self, pitch_dps: float, yaw_dps: float) -> None:
        """Ask for a speed in degrees per second, in the TELEMETRY frame.

        Positive means the reported angle should increase. That is the frame
        every closed-loop caller already works in, because their error term is
        `target - measured`. Operator-facing input goes through `set_axes`,
        which converts; mixing the two frames silently inverts the controls.
        """
        with self._lock:
            self._tilt = pitch_dps
            self._pan = yaw_dps
            self._last_was_stop = (pitch_dps == 0.0 and yaw_dps == 0.0)
            self._last_input = time.monotonic()

    def set_speed_cap(self, dps: float) -> None:
        self.speed_cap = max(response.MIN_DPS, min(response.MAX_DPS, dps))

    def release(self) -> None:
        """The operator letting go. Gets the preset's settle, not a dead stop.

        The tail keeps the pump sending, which also keeps the camera's own
        deadman fed for as long as the head is still moving -- the two would
        otherwise fight, with the deadman parking the head part-way through a
        cinematic stop.
        """
        self.set_rate(0.0, 0.0)

    def _pump(self) -> None:
        idle = True
        last_tick = time.monotonic()
        while not self._stop.wait(STICK_INTERVAL_S):
            now = time.monotonic()
            dt = min(0.2, now - last_tick)       # a scheduling hiccup must not
            last_tick = now                      # be integrated as real time

            with self._lock:
                target_p, target_y = self._tilt, self._pan
                last, was_stop = self._last_input, self._last_was_stop

            # Two different silences, and they must not be treated alike.
            #
            # An operator who centred the stick has said everything they mean
            # to say. The client then goes quiet, and that silence is normal --
            # the tail is this side finishing the instruction it was given. A
            # client that stops mid-move without ever saying stop has failed,
            # and coasting on the last thing it said is exactly wrong.
            #
            # Conflating them truncates the tail at the deadman, which silently
            # defeats every preset whose settle is longer than the timeout --
            # that is, the slow cinematic ones the feature exists for.
            if (now - last) > INPUT_TIMEOUT_S and not was_stop:
                if self.shaper.moving:
                    self.shaper.abort()
                target_p = target_y = 0.0

            pitch_dps, yaw_dps = self.shaper.update(target_p, target_y, dt)

            d0, d1 = self.wire_deflections(pitch_dps, yaw_dps)
            a0, a1 = axis(d0), axis(d1)

            if a0 == CENTER and a1 == CENTER:
                if idle:
                    continue                     # nothing held, nothing to say
                # One centring packet, then quiet.
                idle = True
            else:
                idle = False

            self.link.send_frame(commands.gimbal_stick(a0, a1))

    # -- discrete actions ---------------------------------------------------

    def recenter(self) -> None:
        self.link.send_frame(commands.gimbal_recenter())

    def flip(self) -> None:
        self.link.send_frame(commands.gimbal_flip())

    def follow_mode(self) -> None:
        self.link.send_frame(commands.gimbal_follow())

    def fpv_mode(self) -> None:
        self.link.send_frame(commands.gimbal_fpv())


class MoveReport:
    """What actually happened during a take, as opposed to what was programmed.

    "The director circled it" and "the rig repeated correctly" are different
    judgments and a take log needs both. This records the second one: how far
    the camera ever strayed from its path, whether a target had to be clamped
    at a travel limit, and whether telemetry dropped out mid-move. A take with
    a 9 deg excursion and three clamp events is not a usable match to the take
    before it, however good it looked on the monitor.
    """

    # Above this peak error the move did not track its path closely enough to
    # be called a repeat of another take.
    CLEAN_ERROR_DEG = 3.0

    # Where the camera actually WAS, decimated. Peak error says how far the
    # take strayed from its path; it cannot say whether two takes strayed the
    # same way, and two takes that both wandered identically composite fine
    # while two that wandered oppositely do not. Comparing takes needs the
    # positions, not the summary.
    #
    # Capped so a long take cannot grow the log without bound: a forty minute
    # timelapse at twenty-five hertz would otherwise be sixty thousand points
    # in a take log that gets serialised into every status response.
    TRACE_POINTS = 300

    def __init__(self) -> None:
        self.peak_error_pitch = 0.0
        self.peak_error_yaw = 0.0
        self.clamp_events = 0
        self.telemetry_gaps = 0
        self.samples = 0
        self.cues_waited = 0
        self.elapsed = 0.0
        self.aborted = False
        self.trace: list[tuple[float, float, float]] = []
        self._trace_every = 1
        self._trace_seen = 0

    def note(self, err_pitch: float, err_yaw: float, clamped: bool,
             have_telemetry: bool, t: float | None = None,
             pitch: float | None = None, yaw: float | None = None) -> None:
        self.samples += 1
        if t is not None and pitch is not None and yaw is not None:
            self._record(t, pitch, yaw)
        self.peak_error_pitch = max(self.peak_error_pitch, abs(err_pitch))
        self.peak_error_yaw = max(self.peak_error_yaw, abs(err_yaw))
        if clamped:
            self.clamp_events += 1
        if not have_telemetry:
            self.telemetry_gaps += 1

    def _record(self, t: float, pitch: float, yaw: float) -> None:
        """Keep a bounded, evenly spaced trace however long the take runs.

        When the buffer fills, every other point is dropped and the sampling
        interval doubles. The trace stays evenly spaced and never exceeds the
        cap, without needing to know the take's length in advance -- which for
        a cued move nobody does.
        """
        self._trace_seen += 1
        if self._trace_seen % self._trace_every:
            return
        self.trace.append((round(t, 3), round(pitch, 3), round(yaw, 3)))
        if len(self.trace) > self.TRACE_POINTS:
            # [1::2], not [::2]. The kept points sit at seen-counts
            # 1*every, 2*every, 3*every ... and after doubling the interval
            # the next point appended lands on a multiple of 2*every. Keeping
            # the odd-indexed ones (2*every, 4*every ...) puts the survivors
            # on that same grid, so the join is one full interval like every
            # other gap. Keeping [::2] leaves the boundary gap at half the
            # spacing, and a trace that is evenly spaced except in one place
            # skews an RMS comparison without ever looking wrong.
            self.trace = self.trace[1::2]
            self._trace_every *= 2

    @property
    def peak_error(self) -> float:
        return max(self.peak_error_pitch, self.peak_error_yaw)

    @property
    def verdict(self) -> str:
        if self.aborted:
            return "aborted"
        if not self.samples:
            return "no data"
        if self.telemetry_gaps > self.samples * 0.1:
            return "telemetry loss"
        if self.clamp_events:
            return "hit travel limit"
        if self.peak_error > self.CLEAN_ERROR_DEG:
            return "off path"
        return "clean"

    @property
    def repeatable(self) -> bool:
        return self.verdict == "clean"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "repeatable": self.repeatable,
            "peak_error": round(self.peak_error, 2),
            "peak_error_pitch": round(self.peak_error_pitch, 2),
            "peak_error_yaw": round(self.peak_error_yaw, 2),
            "clamp_events": self.clamp_events,
            "telemetry_gaps": self.telemetry_gaps,
            "cues_waited": self.cues_waited,
            "samples": self.samples,
            "elapsed": round(self.elapsed, 2),
            "aborted": self.aborted,
            "trace": [list(p) for p in self.trace],
        }


class MoveRunner:
    """Play a programmed move by tracking its time-parameterised path.

    Not a "go to point" seek. At every tick the move is sampled for where the
    camera *should be at that instant*, and both axes are driven toward that
    moving target. Tracking the path rather than the endpoint is what makes
    the easing curve visible in the footage instead of being swallowed by the
    controller's own response.

    Pitch and yaw both close on the camera's own 0x04/0x05 telemetry, so the
    move is repeatable: the same programmed move produces the same framing.
    """

    def __init__(self, link, stick: GimbalStick, kp: float = 2.0,
                 limits: moves.SoftLimits | None = None):
        self.link = link
        self.stick = stick
        self.kp = kp
        self.limits = limits or moves.SoftLimits()

        self.move: moves.Move | None = None
        self.started_at: float | None = None
        self.elapsed = 0.0
        self.running = False
        self.error_pitch = 0.0
        self.error_yaw = 0.0
        self.clamped = False
        self.report = MoveReport()

        # Cue state: a cued waypoint stops the clock until a human says GO.
        self.waiting_cue: int | None = None
        self._cue_go = threading.Event()
        self._pause_offset = 0.0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def progress(self) -> float:
        if not self.move or self.move.total_duration <= 0:
            return 0.0
        return min(1.0, self.elapsed / self.move.total_duration)

    def start(self, move: moves.Move) -> None:
        if len(move.waypoints) < 2:
            raise ValueError("a move needs at least two waypoints")
        self.stop()
        self.move = move
        self.report = MoveReport()
        self.waiting_cue = None
        self._pause_offset = 0.0
        self._cue_go.clear()
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, name="move-runner", daemon=True)
        self._thread.start()

    def stop(self, aborted: bool = False) -> None:
        if aborted and self.running:
            self.report.aborted = True
        self._stop.set()
        self._cue_go.set()          # release a waiting cue so the thread exits
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.running = False
        self.waiting_cue = None
        self.stick.release()

    def go(self) -> None:
        """Release a cue hold. The human beat that starts the next segment."""
        if self.waiting_cue is None:
            raise RuntimeError("not waiting at a cue")
        self._cue_go.set()

    def _run(self) -> None:
        move = self.move
        assert move is not None
        start_time = time.monotonic()
        self.started_at = start_time
        period = 1.0 / 40.0
        # Cue points, earliest first. The clock stops at each until GO.
        pending_cues = sorted(move.cue_points())
        cue_i = 0
        try:
            while not self._stop.wait(period):
                self.elapsed = time.monotonic() - start_time - self._pause_offset

                # Reached a cue? Freeze here and hold the frame until released.
                while cue_i < len(pending_cues) and self.elapsed >= pending_cues[cue_i][0]:
                    cue_time, cue_idx = pending_cues[cue_i]
                    cue_i += 1
                    if not self._hold_at_cue(cue_idx, move, cue_time):
                        return
                    self.elapsed = time.monotonic() - start_time - self._pause_offset

                target = move.sample(self.elapsed)
                if target is None:
                    break
                tp, ty = target

                # Path velocity, for the feedforward term below.
                ahead = move.sample(self.elapsed + FF_LOOKAHEAD_S)
                if ahead is None:
                    vp = vy = 0.0
                else:
                    vp = _wrap180(ahead[0] - tp) / FF_LOOKAHEAD_S
                    vy = _wrap180(ahead[1] - ty) / FF_LOOKAHEAD_S

                clamped_p = self.limits.clamp_pitch(tp)
                self.clamped = abs(_wrap180(clamped_p - tp)) > 0.05
                self._drive(clamped_p, ty, vp, vy)
                measured = getattr(self, "_last_measured", None)
                self.report.note(self.error_pitch, self.error_yaw, self.clamped,
                                 getattr(self.link, "attitude", None) is not None,
                                 t=self.elapsed,
                                 pitch=measured[0] if measured else None,
                                 yaw=measured[1] if measured else None)

                if move.finished(self.elapsed):
                    break
        finally:
            self.report.elapsed = self.elapsed
            self.running = False
            self.waiting_cue = None
            # Deliberately NOT returning to the start. Releasing the frame the
            # instant a move ends destroys the tail of the take: reactions,
            # room tone and the editorial handle all live after the last
            # waypoint. The camera holds where it landed until told otherwise.
            self.stick.release()

    def _hold_at_cue(self, index: int, move, cue_time: float) -> bool:
        """Freeze on the current frame until GO. False if we were stopped."""
        self.waiting_cue = index
        self.report.cues_waited += 1
        self._cue_go.clear()
        paused_at = time.monotonic()
        held = move.sample(cue_time)
        while not self._cue_go.wait(0.05):
            if self._stop.is_set():
                self.waiting_cue = None
                return False
            # Keep actively holding the framing rather than drifting.
            if held is not None:
                self._drive(self.limits.clamp_pitch(held[0]), held[1])
        self.waiting_cue = None
        self._pause_offset += time.monotonic() - paused_at
        return not self._stop.is_set()

    def _drive(self, target_pitch: float, target_yaw: float,
               vel_pitch: float = 0.0, vel_yaw: float = 0.0) -> None:
        """Feedforward plus proportional correction.

        Proportional control alone always trails a moving target: it only
        produces output once an error already exists, so a programmed move
        arrives late and undershoots its last waypoint. Feeding the path's own
        velocity forward means the stick is already commanding roughly the
        right rate, and the P term only has to clean up the difference.
        """
        att = getattr(self.link, "attitude", None)
        if att is None:
            return
        self.error_pitch = _wrap180(target_pitch - att.pitch)
        self.error_yaw = _wrap180(target_yaw - att.yaw)

        # Everything here is in degrees per second, including the gain: `kp`
        # answers "how fast should a degree of error be corrected", which is a
        # question with a physical answer. It used to produce a raw deflection,
        # a quantity whose meaning changed across its own range on this camera,
        # so the same gain behaved differently depending on how fast the move
        # already was. The stick applies signs and linearisation.
        self.stick.set_rate(vel_pitch + self.error_pitch * self.kp,
                            vel_yaw + self.error_yaw * self.kp)
        # Remembered for the take log so two takes can be compared later. The
        # measured attitude, not the commanded target: the target is identical
        # between takes by construction and comparing it proves nothing.
        self._last_measured = (att.pitch, att.yaw)


class AttitudeFollower:
    """Drive the gimbal toward an absolute target attitude with a P controller.

    Tilt is closed-loop against the camera's own `0x04/0x05` heartbeat, so it
    settles on a real angle. Pan is open-loop rate control: the MPU6886 has no
    magnetometer, so the Core2 has no absolute yaw to close against -- feed
    `pan_rate` from the gyro instead of an angle.
    """

    def __init__(self, stick: GimbalStick, link: Datalink,
                 kp_tilt: float = 1.5, max_tilt_dps: float = response.MAX_DPS,
                 pan_rate_scale: float = 1.0):
        self.stick = stick
        self.link = link
        # deg/s of correction per degree of error, so a 10 degree error asks
        # for 15 deg/s and settles with a time constant under a second.
        self.kp_tilt = kp_tilt
        self.max_tilt_dps = max_tilt_dps
        # The hand's yaw rate is already in deg/s and the head now accepts
        # deg/s, so this is 1:1 unless an operator deliberately wants leverage.
        self.pan_rate_scale = pan_rate_scale

    def update(self, target_pitch_deg: float, pan_rate_dps: float) -> None:
        measured = self.link.gimbal_pitch
        if measured is None:
            # No telemetry yet: fall back to rate control so the rig still moves.
            tilt_dps = target_pitch_deg * self.kp_tilt
        else:
            # Shortest way round, so a wrap at +/-180 does not slam the stick.
            tilt_dps = _wrap180(target_pitch_deg - measured) * self.kp_tilt
        tilt_dps = max(-self.max_tilt_dps, min(self.max_tilt_dps, tilt_dps))
        # set_rate owns the signs, so this no longer applies TILT_SIGN itself.
        self.stick.set_rate(tilt_dps, pan_rate_dps * self.pan_rate_scale)
