"""Motion timelapse: shoot, move, shoot.

The oldest motion-control trick there is. Rather than creeping the head through
a move in real time, the rig steps to a position, waits for it to stop
wobbling, takes one frame, and steps again. Played back at 25 a second, an
hour becomes twenty seconds and the camera appears to glide.

Two modes, and the difference matters:

* SMS (shoot-move-shoot) stops dead for every frame. Each frame is as sharp as
  the lens allows, and the result can look unnaturally crisp -- there is no
  motion blur anywhere, so fast movement in the frame strobes.
* CONTINUOUS keeps moving through the exposure. Each frame carries a little
  blur in the direction of travel, which is what makes the playback read as
  movement rather than as a slideshow. It needs a slow enough crawl that the
  blur stays within a frame's worth.

The arithmetic below is the part that goes wrong in the field, and all of it
is knowable before a four-hour shoot rather than after it:

* how long the shoot will actually take, which is never what people guess;
* how much of the move each frame advances, because past roughly a degree a
  frame the playback judders no matter how good the head is;
* whether the whole thing outlasts the battery or the card.

Nothing here talks to the camera. It plans; the runner executes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from . import moves

# Playback rates an editor will actually conform to.
OUTPUT_RATES = (23.976, 24.0, 25.0, 30.0, 50.0, 60.0)
DEFAULT_OUTPUT_FPS = 25.0

# Time for the head to stop ringing after a step. Measured behaviour on this
# gimbal is a settle well under half a second for the small steps a timelapse
# uses; a third is comfortable and cheap at these frame counts.
DEFAULT_SETTLE_S = 0.35

# Past about this much angular travel per frame the playback judders: the
# subject jumps further between frames than the eye will merge. It is the
# single most useful number in the whole plan and the one nobody computes.
JUDDER_DEG_PER_FRAME = 1.0

# Below this the move is so fine that the head's own dead band eats it and
# some frames do not move at all, which reads as a stutter.
STALL_DEG_PER_FRAME = 0.02

MAX_FRAMES = 10000
# A stale unattended shoot is recoverable; one longer than a day is almost
# certainly an accidental request or a plan that cannot be supervised safely.
MAX_SHOOT_S = 24 * 60 * 60
# Continuous movement needs a real clock interval.  Faster requests both
# outrun the shutter/gimbal hand-off and make a zero-length retime possible.
MIN_CONTINUOUS_INTERVAL_S = 0.2


class TimelapseError(ValueError):
    """A plan that cannot be shot."""


def _finite_number(value, name: str) -> float:
    """Strict numeric boundary for operator and persisted plan data."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelapseError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise TimelapseError(f"{name} must be a finite number")
    return value


def _canonical_move(move: moves.Move) -> moves.Move:
    """Clone through the canonical path validator without touching the source."""
    if not isinstance(move, moves.Move):
        raise TimelapseError("move must be a Move")
    try:
        return moves.Move.from_dict(move.to_dict())
    except (TypeError, ValueError, OverflowError) as exc:
        raise TimelapseError(f"invalid move: {exc}") from None


def _finite_json(value) -> bool:
    """Whether a decoded progress payload contains only finite JSON values."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return True
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item)
                   for key, item in value.items())
    return False


def _reject_ambiguous_continuous(move: moves.Move) -> None:
    if move.loop:
        raise TimelapseError("continuous timelapse refuses looped paths")
    if move.ping_pong:
        raise TimelapseError("continuous timelapse refuses ping-pong paths")
    if move.has_cues:
        raise TimelapseError("continuous timelapse refuses cue-driven paths")


@dataclass(frozen=True)
class Plan:
    frames: int
    settle_s: float
    expose_s: float
    gap_s: float
    output_fps: float
    mode: str                       # "sms" | "continuous"
    pitch_span: float               # degrees travelled, total
    yaw_span: float

    @property
    def per_frame_s(self) -> float:
        """Wall-clock cost of one frame."""
        if self.mode == "continuous":
            # Nothing stops, so no settle is paid.
            return self.expose_s + self.gap_s
        return self.settle_s + self.expose_s + self.gap_s

    @property
    def shoot_s(self) -> float:
        return self.frames * self.per_frame_s

    @property
    def playback_s(self) -> float:
        return self.frames / self.output_fps

    @property
    def speed_ratio(self) -> float:
        """How many times faster the playback runs than reality."""
        return self.shoot_s / self.playback_s if self.playback_s else 0.0

    @property
    def pitch_per_frame(self) -> float:
        return abs(self.pitch_span) / max(1, self.frames - 1)

    @property
    def yaw_per_frame(self) -> float:
        return abs(self.yaw_span) / max(1, self.frames - 1)

    @property
    def worst_per_frame(self) -> float:
        return max(self.pitch_per_frame, self.yaw_per_frame)

    def to_dict(self) -> dict:
        return {
            "frames": self.frames, "mode": self.mode,
            "settle_s": self.settle_s, "expose_s": self.expose_s,
            "gap_s": self.gap_s, "output_fps": self.output_fps,
            "per_frame_s": round(self.per_frame_s, 3),
            "shoot_s": round(self.shoot_s, 1),
            "playback_s": round(self.playback_s, 2),
            "speed_ratio": round(self.speed_ratio, 1),
            "pitch_per_frame": round(self.pitch_per_frame, 4),
            "yaw_per_frame": round(self.yaw_per_frame, 4),
            "warnings": self.warnings(),
        }

    def warnings(self) -> list[str]:
        """Everything worth knowing before committing hours to this."""
        out: list[str] = []
        worst = self.worst_per_frame
        if worst > JUDDER_DEG_PER_FRAME:
            out.append(
                f"{worst:.2f} deg per frame will judder on playback -- the "
                f"subject jumps further between frames than the eye merges. "
                f"Use more frames or a shorter move (aim under "
                f"{JUDDER_DEG_PER_FRAME:.1f})."
            )
        if 0 < worst < STALL_DEG_PER_FRAME:
            out.append(
                f"{worst:.3f} deg per frame is below the head's dead band, so "
                f"some frames will not move at all and the result will stutter."
            )
        if self.shoot_s > 3600:
            out.append(
                f"the shoot runs {self.shoot_s / 3600:.1f} hours for "
                f"{self.playback_s:.0f}s of footage -- check battery and card."
            )
        if self.mode == "sms":
            out.append(
                "shoot-move-shoot gives no motion blur at all; fast movement "
                "in frame will strobe. Continuous mode blurs with the travel."
            )
        if self.playback_s < 2.0:
            out.append(
                f"{self.playback_s:.1f}s of footage is shorter than most cuts "
                f"-- {int(2.0 * self.output_fps)} frames would give two seconds."
            )
        return out


def plan_for(move: moves.Move, frames: int,
             expose_s: float = 0.5,
             settle_s: float = DEFAULT_SETTLE_S,
             gap_s: float = 0.2,
             output_fps: float = DEFAULT_OUTPUT_FPS,
             mode: str = "sms") -> Plan:
    """Work out what shooting `move` as `frames` stills actually costs."""
    if mode not in ("sms", "continuous"):
        raise TimelapseError(f"unknown mode {mode!r}")
    if type(frames) is not int or not (2 <= frames <= MAX_FRAMES):
        raise TimelapseError(f"frames must be between 2 and {MAX_FRAMES}")
    expose_s = _finite_number(expose_s, "expose time")
    settle_s = _finite_number(settle_s, "settle time")
    gap_s = _finite_number(gap_s, "gap time")
    output_fps = _finite_number(output_fps, "output frame rate")
    if output_fps <= 0:
        raise TimelapseError("output frame rate must be positive")
    for name, v in (("expose", expose_s), ("settle", settle_s), ("gap", gap_s)):
        if v < 0:
            raise TimelapseError(f"{name} time cannot be negative")

    interval = expose_s + gap_s
    if interval <= 0:
        raise TimelapseError("frame interval must be positive")
    if mode == "continuous":
        if interval < MIN_CONTINUOUS_INTERVAL_S:
            raise TimelapseError(
                f"continuous frame interval must be at least "
                f"{MIN_CONTINUOUS_INTERVAL_S:g} seconds")
        move = _canonical_move(move)
        _reject_ambiguous_continuous(move)
    else:
        move = _canonical_move(move)

    poses = frame_poses(move, frames)
    if len(poses) < 2:
        raise TimelapseError("a timelapse needs a move with at least two waypoints")

    pitch_span = sum(abs(moves.wrap180(b[0] - a[0]))
                     for a, b in zip(poses, poses[1:]))
    yaw_span = sum(abs(moves.wrap180(b[1] - a[1]))
                   for a, b in zip(poses, poses[1:]))

    plan = Plan(frames=frames, settle_s=settle_s, expose_s=expose_s,
                gap_s=gap_s, output_fps=output_fps, mode=mode,
                pitch_span=pitch_span, yaw_span=yaw_span)
    if plan.shoot_s > MAX_SHOOT_S:
        raise TimelapseError(
            f"timelapse duration must not exceed {MAX_SHOOT_S / 3600:g} hours")
    return plan


def continuous_plan_move(move: moves.Move, plan: Plan) -> moves.Move:
    """Return the finite, retimed canonical path for a continuous shoot.

    Frames are exposed at ``i * (expose_s + gap_s)``.  The authored path is
    scaled into the interval from frame zero through the final exposure; a
    final interval is then retained as dwell so the runner has an honest
    finish window.  It deliberately reuses ``Move`` and its sampler: adding a
    second interpolation engine would make the planned and executed shots
    disagree.
    """
    if not isinstance(plan, Plan):
        raise TimelapseError("continuous timelapse requires a Plan")
    if plan.mode != "continuous":
        raise TimelapseError("continuous timelapse requires a continuous plan")
    # Revalidate an in-memory Plan: persisted data and callers can construct a
    # dataclass directly instead of going through plan_for.
    checked = plan_for(move, plan.frames, expose_s=plan.expose_s,
                       settle_s=plan.settle_s, gap_s=plan.gap_s,
                       output_fps=plan.output_fps, mode=plan.mode)
    source = _canonical_move(move)
    _reject_ambiguous_continuous(source)
    interval = checked.expose_s + checked.gap_s
    traversal_s = (checked.frames - 1) * interval
    # The final dwell belongs to the final-frame completion window, not path
    # traversal.  Every earlier leg/dwell keeps its authored proportion.
    traversal_source_s = source.total_duration - source.waypoints[-1].dwell
    if not math.isfinite(traversal_source_s) or traversal_source_s <= 0:
        raise TimelapseError("continuous timelapse needs a finite nonzero path")
    factor = traversal_s / traversal_source_s
    if not math.isfinite(factor) or factor <= 0:
        raise TimelapseError("continuous timelapse retime is impossible")
    # Move.retimed clamps short legs, which would quietly alter this exact
    # shutter schedule.  Refuse instead of making a different shot.
    if any(w.duration * factor < moves.MIN_LEG_S for w in source.waypoints[1:]):
        raise TimelapseError("continuous frame interval is too short for this path")

    out = _canonical_move(source)
    for waypoint in out.waypoints[:-1]:
        waypoint.duration *= factor
        waypoint.dwell *= factor
    out.waypoints[-1].duration *= factor
    # Preserve all authored dwell/flow/zoom timing through traversal, then
    # reserve precisely one interval at the final frame for completion.
    out.waypoints[-1].dwell = interval
    return out


def frame_poses(move: moves.Move, frames: int) -> list[tuple[float, float]]:
    """Where the head sits for each frame.

    Sampled from the move's own timeline rather than by dividing the angles
    evenly, so the easing is preserved: a move authored to ease in and out
    produces a timelapse that eases in and out. Dividing the ANGLE evenly
    instead -- the obvious implementation -- throws the easing away and gives
    a constant-rate glide, which is a different shot.
    """
    total = move.total_duration
    if len(move.waypoints) < 2 or total <= 0 or frames < 2:
        return []
    out = []
    for i in range(frames):
        got = move.sample(total * i / (frames - 1))
        if got is not None:
            out.append(got)
    return out


def frames_for_playback(seconds: float, output_fps: float = DEFAULT_OUTPUT_FPS) -> int:
    """How many frames a wanted playback length needs.

    The direction people actually think in: "I want eight seconds", not "I
    want two hundred frames".
    """
    if seconds <= 0 or output_fps <= 0:
        raise TimelapseError("playback length and frame rate must be positive")
    return max(2, round(seconds * output_fps))


# --- surviving the shoot -----------------------------------------------------
#
# A four-hour timelapse meets a battery swap, a dropped access point, or
# somebody closing the laptop. Losing the shoot to any of those is losing the
# afternoon, and the frames already on the card are useless without the ones
# that follow.
#
# Progress is written after every frame so a restart knows where it stopped.
# Two things make a resume safe rather than merely possible:
#
#   * the move is FINGERPRINTED. Resuming frame 812 of a move that has since
#     been edited would step the head somewhere the earlier frames never went,
#     and the join would be invisible until the sequence played back.
#   * a resume always demands re-referencing. After a power cycle the head has
#     re-homed and the stored angles may no longer mean what they meant, so the
#     operator has to put the camera back on frame one and say so.

import hashlib
import json
import os
from pathlib import Path

STATE_NAME = "timelapse-progress.json"

# A resume older than this is almost certainly a forgotten shoot rather than an
# interrupted one, and silently offering it invites resuming into a scene that
# was struck hours ago.
STALE_AFTER_S = 12 * 3600


def fingerprint(move: moves.Move) -> str:
    """Identity of the PATH, not of the file.

    Renaming a move or editing its notes must not invalidate a resume; moving
    a waypoint must. Only the things that change where the head goes are in
    the hash.
    """
    move = _canonical_move(move)
    body = [
        [round(w.pitch, 3), round(w.yaw, 3), round(w.duration, 4),
         round(w.dwell, 4), w.easing, bool(w.flow),
         None if w.zoom is None else round(w.zoom, 4), w.zoom_easing,
         bool(w.cue)]
        for w in move.waypoints
    ]
    body.append([bool(move.loop), bool(move.ping_pong), bool(move.route_arcs)])
    # Keep legacy fingerprints stable; curve edits must invalidate a resume.
    if any(w.has_axis_curves for w in move.waypoints):
        body.append([[w.pitch_curve, w.yaw_curve, w.axis_link] for w in move.waypoints])
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def save_progress(directory, move: moves.Move, plan: Plan, frame: int,
                  now: float) -> Path:
    """Record where the shoot got to. Written atomically: an interrupted write
    during a power loss is exactly the moment this file matters."""
    path = Path(directory) / STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint(move),
        "frame": int(frame),
        "frames": int(plan.frames),
        "plan": plan.to_dict(),
        "move": move.to_dict(),
        "at": float(now),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_progress(directory, move: moves.Move | None = None,
                  now: float | None = None) -> dict | None:
    """The interrupted shoot, if there is one worth resuming.

    Returns None rather than raising: a missing, unreadable or stale file is
    the ordinary case, and a startup that fails because of a leftover
    diagnostic would be worse than the thing it is guarding.
    """
    path = Path(directory) / STATE_NAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if (not isinstance(payload, dict) or "frame" not in payload
            or not _finite_json(payload)):
        return None
    try:
        # These are consumed by resume directly.  Validate the whole boundary
        # here so a hand-edited or interrupted state file cannot make startup
        # crash later on a NaN, bool-as-int, or unexpected nested object.
        frame = payload["frame"]
        frames = payload["frames"]
        if (type(frame) is not int or type(frames) is not int
                or not 0 <= frame <= frames
                or not 2 <= frames <= MAX_FRAMES):
            return None
        if not isinstance(payload.get("fingerprint"), str):
            return None
        at = _finite_number(payload.get("at"), "saved time")
        stored_move = _canonical_move(moves.Move.from_dict(payload.get("move")))
        plan_data = payload.get("plan")
        if not isinstance(plan_data, dict):
            return None
        stored_plan = plan_for(
            stored_move, plan_data.get("frames"),
            expose_s=plan_data.get("expose_s"),
            settle_s=plan_data.get("settle_s"),
            gap_s=plan_data.get("gap_s"),
            output_fps=plan_data.get("output_fps"),
            mode=plan_data.get("mode"),
        )
        if stored_plan.frames != frames:
            return None
        if now is not None:
            now = _finite_number(now, "current time")
        if move is not None:
            matches = fingerprint(move) == payload["fingerprint"]
    except (KeyError, TypeError, ValueError, OverflowError, TimelapseError):
        return None

    payload["stale"] = False if now is None else (now - at) > STALE_AFTER_S
    payload["finished"] = frame >= frames
    if move is not None:
        payload["matches"] = matches
    return payload


def clear_progress(directory) -> None:
    """Called on a clean finish. A completed shoot offering to resume itself
    is a trap the morning after."""
    try:
        (Path(directory) / STATE_NAME).unlink()
    except OSError:
        pass


def remaining_poses(move: moves.Move, plan: Plan, done: int
                    ) -> list[tuple[float, float]]:
    """The frames still to shoot.

    Sampled from the full move and then sliced, never re-sampled over the
    remainder: re-sampling would redistribute the frames across what is left
    and every remaining position would land somewhere the original plan never
    intended, which on playback reads as a speed change halfway through.
    """
    poses = frame_poses(move, plan.frames)
    return poses[max(0, int(done)):]
