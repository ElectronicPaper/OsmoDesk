"""Motion-control primitives: waypoints, easing, timed paths, soft limits.

Pure maths, no hardware and no I/O, so the interesting parts are testable.

The model follows what professional motion control converges on: a move is a
list of waypoints, each an axis position plus the time to reach it and a time
to dwell there, and the path between them is time-parameterised with an easing
curve. MRMC Flair calls these keyframes, the DJI Ronin app calls them Track
waypoints with Duration and Stay Time, eMotimo calls it ramping. Same idea.

Straight linear interpolation reads as mechanical on screen. Real camera moves
accelerate and decelerate, so easing is not decoration here -- it is the thing
that makes a programmed move look shot rather than driven.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

# --- angles -----------------------------------------------------------------


def wrap180(delta: float) -> float:
    """Shortest signed angle difference in degrees."""
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    return delta


def lerp_angle(a: float, b: float, s: float) -> float:
    """Interpolate the short way round, so a move across +/-180 does not
    unwind the long way."""
    return a + wrap180(b - a) * s


# --- easing -----------------------------------------------------------------
# Each takes and returns 0..1. Names match what operators expect to see.


def _clamp01(s: float) -> float:
    return 0.0 if s < 0.0 else 1.0 if s > 1.0 else s


def _bool_field(data: dict, name: str, default: bool) -> bool:
    """Read a persisted/API boolean without Python truthiness surprises."""
    value = data.get(name, default)
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def linear(s: float) -> float:
    return _clamp01(s)


def ease_in(s: float) -> float:
    """Slow departure, arrives at full speed. Good into a cut."""
    s = _clamp01(s)
    return s * s


def ease_out(s: float) -> float:
    """Full speed away, settles gently. Good out of a cut."""
    s = _clamp01(s)
    return 1.0 - (1.0 - s) ** 2


def ease_in_out(s: float) -> float:
    """The S-curve. The default for a move that starts and ends at rest."""
    s = _clamp01(s)
    return 2 * s * s if s < 0.5 else 1 - ((-2 * s + 2) ** 2) / 2


def ease_in_out_cubic(s: float) -> float:
    """Stronger S-curve: longer ramp, shorter middle. Reads as more deliberate."""
    s = _clamp01(s)
    return 4 * s * s * s if s < 0.5 else 1 - ((-2 * s + 2) ** 3) / 2


def ease_in_out_sine(s: float) -> float:
    """Gentlest S-curve. Closest to a good operator on wheels."""
    return -(math.cos(math.pi * _clamp01(s)) - 1) / 2


EASINGS = {
    "linear": linear,
    "ease-in": ease_in,
    "ease-out": ease_out,
    "ease-in-out": ease_in_out,
    "ease-in-out-cubic": ease_in_out_cubic,
    "ease-in-out-sine": ease_in_out_sine,
}
DEFAULT_EASING = "ease-in-out-sine"


def get_easing(name: str):
    try:
        return EASINGS[name]
    except KeyError:
        raise ValueError(f"unknown easing: {name!r}") from None


# --- monotone cubic interpolation -------------------------------------------
# Easing shapes one leg between two waypoints, and every easing curve in the
# table above starts and ends at zero velocity. That is right for a two-point
# move and wrong for everything longer: a five-node move eases to a dead stop
# five times, which on screen reads as a stutter at each node rather than one
# continuous move. Dwell of zero does not help -- the stop is in the curve.
#
# A node marked `flow` is passed through at speed instead. That needs a curve
# with a non-zero velocity at the node and the same velocity on both sides of
# it, so the two legs join smoothly: cubic Hermite, with a tangent chosen per
# node rather than forced to zero.
#
# The tangents are Fritsch-Carlson limited, which makes the result monotone:
# the curve between two nodes never leaves the interval between their values.
# That matters more here than the smoothness does. Catmull-Rom is the usual
# choice for a pass-through spline and it overshoots on purpose -- pleasant on
# a graph, and on this rig it means a move whose nodes are all inside the
# travel arc can still swing past a hard stop between them. Monotone cannot.


def _fritsch_carlson(secants: list[float], tangents: list[float]) -> list[float]:
    """Limit tangents so each span stays monotone (no overshoot)."""
    out = list(tangents)
    for i, d in enumerate(secants):
        if d == 0.0:
            # Flat span: pin both ends or the curve bulges out of it.
            out[i] = 0.0
            out[i + 1] = 0.0
            continue
        alpha = out[i] / d
        beta = out[i + 1] / d
        if alpha < 0.0:
            out[i] = 0.0
            alpha = 0.0
        if beta < 0.0:
            out[i + 1] = 0.0
            beta = 0.0
        mag = alpha * alpha + beta * beta
        if mag > 9.0:
            scale = 3.0 / math.sqrt(mag)
            out[i] = scale * alpha * d
            out[i + 1] = scale * beta * d
    return out


def hermite_tangents(values: list[float], durations: list[float],
                     flow: list[bool]) -> list[float]:
    """Per-node slopes in units per second.

    `durations[i]` is the time from node i to node i+1. A node that is not
    `flow` gets a zero tangent, which is exactly the old behaviour -- it comes
    to rest there. The ends of a move are always at rest whatever they claim,
    because a move that begins or ends mid-swing cannot be repeated.
    """
    n = len(values)
    if n < 2:
        return [0.0] * n
    secants = [
        (values[i + 1] - values[i]) / durations[i] if durations[i] > 0 else 0.0
        for i in range(n - 1)
    ]
    tangents = [0.0] * n
    for i in range(1, n - 1):
        if not flow[i]:
            continue
        # Weighted by the neighbouring durations, so a short leg next to a long
        # one does not drag the slope towards the long one's average.
        h0, h1 = durations[i - 1], durations[i]
        if h0 + h1 > 0:
            tangents[i] = (h1 * secants[i - 1] + h0 * secants[i]) / (h0 + h1)
    return _fritsch_carlson(secants, tangents)


def hermite(s: float, y0: float, y1: float,
            m0: float, m1: float, h: float) -> float:
    """Cubic Hermite on 0..1, with tangents given per second over a span h."""
    s2 = s * s
    s3 = s2 * s
    return ((2 * s3 - 3 * s2 + 1) * y0
            + (s3 - 2 * s2 + s) * h * m0
            + (-2 * s3 + 3 * s2) * y1
            + (s3 - s2) * h * m1)


# --- soft limits ------------------------------------------------------------


@dataclass(frozen=True)
class SoftLimits:
    """Keep commanded targets inside the axis's real travel.

    Pitch travel is an ARC, not a min/max range, and it crosses the +/-180
    wrap. Measured on an Osmo Pocket 4 Pro by driving each way to the stop:

        +64.5 deg  --(through +180 / -180)-->  -136.9 deg     158.6 deg total

    So a naive `min <= p <= max` test is wrong in both directions: it rejects
    every legal position past the wrap and accepts the whole forbidden 201 deg
    on the other side. That mistake clamped an entire programmed move.

    The arc is stored as a start angle and the sweep from it in the increasing
    direction. `margin` pulls both ends in so a move eases to a stop rather
    than grinding into one.
    """

    arc_from: float = 64.5
    arc_span: float = 158.6
    margin: float = 3.0

    @property
    def usable_span(self) -> float:
        return max(0.0, self.arc_span - 2 * self.margin)

    @property
    def start(self) -> float:
        return wrap180(self.arc_from + self.margin)

    @property
    def end(self) -> float:
        return wrap180(self.arc_from + self.arc_span - self.margin)

    def _offset(self, pitch: float) -> float:
        """How far `pitch` sits along the arc from its start, 0..360."""
        return (pitch - self.start) % 360.0

    def contains(self, pitch: float) -> bool:
        return self._offset(pitch) <= self.usable_span

    def clamp_pitch(self, pitch: float) -> float:
        """Nearest reachable angle, measured the short way round."""
        if self.contains(pitch):
            return pitch
        to_start = (self.start - pitch) % 360.0
        to_end = (pitch - self.end) % 360.0
        return self.start if to_start <= to_end else self.end

    def pitch_blocked(self, pitch: float) -> bool:
        return not self.contains(pitch)

    # The type is named for pitch because pitch was the axis that needed it
    # first, but an arc is an arc. Yaw needs exactly the same treatment and
    # needs it more: its forbidden wedge is 93 degrees wide.
    def offset(self, angle: float) -> float:
        """How far along the usable arc `angle` sits, 0..360."""
        return self._offset(angle)

    def travel(self, a: float, b: float) -> float | None:
        """Signed degrees from `a` to `b` STAYING INSIDE the arc.

        None when either end is outside, so the caller can fall back to the
        ordinary short-way interpolation rather than inventing a path through
        a region the head cannot reach.

        This is the fix for a bug that no amount of node-checking would find:
        `lerp_angle` goes the geometrically short way, and on the yaw axis the
        short way between two perfectly legal angles either side of the dead
        wedge runs straight through it. Every node legal, path illegal, and
        nothing in the authoring UI shows it.
        """
        oa, ob = self._offset(a), self._offset(b)
        if oa > self.usable_span or ob > self.usable_span:
            return None
        return ob - oa

    def at_offset(self, offset: float) -> float:
        """The angle `offset` degrees along the arc from its start."""
        return wrap180(self.start + offset)


# Measured on an Osmo Pocket 4 Pro by driving each axis to both stops. These
# are the single source of truth for the travel geometry: driver/limits.py
# derives its own arcs from them rather than restating the numbers, because
# two copies of a measurement are two copies to forget to update after the head
# is serviced.
#
#   pitch   +64.5 --(through +/-180)--> -136.9    158.6 deg
#   yaw     -48.1 --(through +/-180)--> -134.9    273.2 deg, leaving 86.8 deg
#                                                 behind the camera unreachable
# Shortest leg the sampler will accept. It divides by duration, and a zero
# leg from an aggressive retime would be a division by zero mid-take.
MIN_LEG_S = 0.05

# How far from the first node still counts as "at the start". Half a degree is
# roughly ten pixels on this lens -- tight enough to catch a knocked tripod,
# loose enough not to reject a head that has settled a hair off.
START_TOLERANCE_DEG = 0.5

PITCH_LIMITS = SoftLimits(arc_from=64.5, arc_span=158.6, margin=3.0)
YAW_LIMITS = SoftLimits(arc_from=-48.1, arc_span=273.2, margin=3.0)


def arc_delta(arc: SoftLimits | None, a: float, b: float) -> float:
    """Signed travel from `a` to `b`, along the arc when one is given.

    Falls back to the short way when there is no arc or when either end is
    already outside it -- a move authored outside the travel cannot be routed
    inside it, and pre-flight reports that separately rather than this
    silently relocating the move.
    """
    if arc is not None:
        along = arc.travel(a, b)
        if along is not None:
            return along
    return wrap180(b - a)


# --- waypoints and moves ----------------------------------------------------


@dataclass
class Waypoint:
    """One programmed camera position.

    `duration` is the time to travel here from the previous waypoint, and
    `dwell` is the hold once arrived -- the Duration / Stay Time pair from the
    Ronin app. The first waypoint in a move is the start, so its duration is
    ignored.
    """

    name: str
    pitch: float
    yaw: float
    duration: float = 3.0
    dwell: float = 0.0
    easing: str = DEFAULT_EASING
    zoom: float | None = None
    """Zoom at this node, 0..1, or None to leave the lens alone.

    Zoom rides the same nodes as the move because that is how it is authored --
    you frame the shot, you capture it, and the framing includes how tight it
    was. It is a separate execution track though: the gimbal is a servo that
    takes a continuous rate, and zoom on this camera is a stepped command. So
    the path is sampled continuously here and the driver decides when to spend
    a step.
    """
    zoom_easing: str = DEFAULT_EASING
    """Zoom does not have to share the move's easing, and usually should not.

    A push that starts with the pan and ends with it reads as one gesture; a
    push that lags the pan and catches up at the end reads as a reveal. Same
    two nodes, different shot.
    """
    flow: bool = False
    """Pass through at speed instead of coming to rest here.

    The difference between a move that stops at every node and one continuous
    move. Off by default: an existing move keeps the timing it was cut with.
    """
    cue: bool = False
    """Hold here until an operator presses GO, instead of running on the clock.

    A fixed dwell is fine for a product pass. Anything with a performance in it
    runs on the actor, not a stopwatch: you wait for the line, the door, the
    look, and only then execute the move. A cue point makes the pause
    indefinite and human-released, which is the difference between a rig that
    can shoot drama and one that can only shoot tabletop.
    """

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Waypoint":
        return Waypoint(
            name=str(d.get("name", "")),
            pitch=float(d["pitch"]),
            yaw=float(d["yaw"]),
            duration=max(0.1, float(d.get("duration", 3.0))),
            dwell=max(0.0, float(d.get("dwell", 0.0))),
            easing=str(d.get("easing", DEFAULT_EASING)),
            zoom=None if d.get("zoom") is None else _clamp01(float(d["zoom"])),
            zoom_easing=str(d.get("zoom_easing", DEFAULT_EASING)),
            flow=_bool_field(d, "flow", False),
            cue=_bool_field(d, "cue", False),
        )


@dataclass
class Move:
    """An ordered list of waypoints, played as one shot."""

    name: str = "untitled"
    waypoints: list[Waypoint] = field(default_factory=list)
    loop: bool = False
    ping_pong: bool = False
    route_arcs: bool = True
    """Interpolate along the travel arc rather than the short way round.

    On by default because the alternative is a bug: between two legal yaw
    angles either side of the 87 degree dead wedge, the short way runs through
    the wedge. Turn it off only to reproduce how an old move played.
    """
    setup: dict = field(default_factory=dict)
    """How the camera was rigged. Angles alone do not recreate a frame.

    Two takes a week apart match only if the support height, the base
    orientation, the route, the zoom and any screwed-on glass match too. This
    travels with the shot file so the frame is reproducible, not just the
    gimbal position.
    """

    # -- timing ------------------------------------------------------------

    def arrival_times(self) -> list[float]:
        """When each waypoint is reached, in nominal (uncued) seconds."""
        if not self.waypoints:
            return []
        times = [0.0]
        cursor = self.waypoints[0].dwell
        for wp in self.waypoints[1:]:
            cursor += wp.duration
            times.append(cursor)
            cursor += wp.dwell
        return times

    def cue_points(self) -> list[tuple[float, int]]:
        """(arrival time, waypoint index) for every waypoint that waits for GO."""
        return [(t, i) for i, t in enumerate(self.arrival_times())
                if self.waypoints[i].cue]

    @property
    def has_cues(self) -> bool:
        return any(w.cue for w in self.waypoints)

    @property
    def legs(self) -> list[tuple[Waypoint, Waypoint]]:
        return list(zip(self.waypoints, self.waypoints[1:]))

    @property
    def uses_flow(self) -> bool:
        """Any pass-through node switches the whole move to the spline.

        All-or-nothing per move rather than per leg: the tangent at a node is
        shared by the legs either side of it, so the two paths cannot be mixed
        within one move without the join disagreeing with itself. A move with
        no flow nodes takes the original leg-by-leg path exactly, so anything
        already cut keeps its timing.
        """
        return any(w.flow for w in self.waypoints)

    def _arc(self, attr: str) -> SoftLimits | None:
        if not self.route_arcs:
            return None
        return PITCH_LIMITS if attr == "pitch" else YAW_LIMITS

    def _durations(self) -> list[float]:
        return [w.duration for w in self.waypoints[1:]]

    def _unwrapped(self, attr: str) -> list[float]:
        """Node values on a continuous line rather than modulo 360.

        Tangents are slopes, and a slope across the +/-180 seam computed on
        wrapped values is wrong by 360 degrees per second. Unwrap first, sample,
        and let the caller wrap the result.
        """
        arc = self._arc(attr)
        vals = [getattr(self.waypoints[0], attr)]
        for prev, nxt in self.legs:
            vals.append(vals[-1] + arc_delta(arc, getattr(prev, attr),
                                             getattr(nxt, attr)))
        return vals

    def _tangents(self, attr: str) -> list[float]:
        return hermite_tangents(self._unwrapped(attr), self._durations(),
                                [w.flow for w in self.waypoints])

    @property
    def total_duration(self) -> float:
        """Whole move including dwells. The first waypoint's dwell counts."""
        if len(self.waypoints) < 2:
            return 0.0
        total = self.waypoints[0].dwell
        for _, end in self.legs:
            total += end.duration + end.dwell
        return total

    def sample(self, t: float) -> tuple[float, float] | None:
        """Position at time `t` seconds, or None once the move has finished.

        Walks the timeline leg by leg. Dwells hold the previous position, so a
        Stay Time reads as a genuine pause rather than a slow crawl.
        """
        if len(self.waypoints) < 2:
            return None
        if t < 0:
            t = 0.0

        # Computed before the early returns below, not after: a dwell hold and
        # the end-of-move value have to agree with the legs about which turn of
        # the circle the move is on. Handing back the raw waypoint angle at the
        # end of a seam-crossing move put a 360 degree step in the path -- the
        # legs had walked 170 -> 190 -> 210 and the terminal value then said
        # -150.
        flowing = self.uses_flow
        if flowing:
            pitch_v = self._unwrapped("pitch")
            yaw_v = self._unwrapped("yaw")
            pitch_m = self._tangents("pitch")
            yaw_m = self._tangents("yaw")

        total = self.total_duration
        if self.ping_pong and total > 0:
            cycle = total * 2
            t = t % cycle if self.loop else t
            if t > total:
                t = cycle - t  # reverse leg
        elif self.loop and total > 0:
            t = t % total
        elif t >= total:
            if flowing:
                return (pitch_v[-1], yaw_v[-1])
            return (self.waypoints[-1].pitch, self.waypoints[-1].yaw)

        cursor = 0.0
        first = self.waypoints[0]
        if t < first.dwell:
            return (first.pitch, first.yaw)
        cursor += first.dwell

        for i, (start, end) in enumerate(self.legs):
            if t < cursor + end.duration:
                s = (t - cursor) / end.duration if end.duration > 0 else 1.0
                if flowing:
                    # Returned unwrapped, like lerp_angle above: a move that
                    # crosses the +/-180 seam yields 190 rather than -170, and
                    # every consumer already wrap180s the difference. Wrapping
                    # here instead would put a 360 degree step in the middle of
                    # an otherwise smooth path, which is exactly the thing the
                    # feedforward term differentiates.
                    h = end.duration
                    return (
                        hermite(s, pitch_v[i], pitch_v[i + 1],
                                pitch_m[i], pitch_m[i + 1], h),
                        hermite(s, yaw_v[i], yaw_v[i + 1],
                                yaw_m[i], yaw_m[i + 1], h),
                    )
                shaped = get_easing(end.easing)(s)
                return (
                    start.pitch + arc_delta(self._arc("pitch"),
                                            start.pitch, end.pitch) * shaped,
                    start.yaw + arc_delta(self._arc("yaw"),
                                          start.yaw, end.yaw) * shaped,
                )
            cursor += end.duration
            if t < cursor + end.dwell:
                if flowing:
                    return (pitch_v[i + 1], yaw_v[i + 1])
                return (end.pitch, end.yaw)
            cursor += end.dwell

        if flowing:
            return (pitch_v[-1], yaw_v[-1])
        return (self.waypoints[-1].pitch, self.waypoints[-1].yaw)

    @property
    def has_zoom(self) -> bool:
        return any(w.zoom is not None for w in self.waypoints)

    def zoom_nodes(self) -> list[tuple[float, float, str]]:
        """(arrival time, zoom, easing) for the nodes that set a zoom.

        Nodes that leave zoom as None are skipped rather than treated as 0.
        Authoring a move usually means framing a few key positions and letting
        the rest ride, and reading an unset node as fully wide would rack the
        lens out between two matched framings.
        """
        return [(t, w.zoom, w.zoom_easing)
                for t, w in zip(self.arrival_times(), self.waypoints)
                if w.zoom is not None]

    def sample_zoom(self, t: float) -> float | None:
        """Zoom at time `t`, or None if this move does not drive the lens.

        Held flat before the first zoom node and after the last: a move that
        only sets zoom in its middle should not creep the lens on the way in.
        """
        nodes = self.zoom_nodes()
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0][1]

        total = self.total_duration
        if total > 0:
            if self.ping_pong:
                cycle = total * 2
                t = t % cycle if self.loop else t
                if t > total:
                    t = cycle - t
            elif self.loop:
                t = t % total

        if t <= nodes[0][0]:
            return nodes[0][1]
        for (t0, z0, _), (t1, z1, ease) in zip(nodes, nodes[1:]):
            if t < t1:
                span = t1 - t0
                s = (t - t0) / span if span > 0 else 1.0
                return z0 + (z1 - z0) * get_easing(ease)(s)
        return nodes[-1][1]

    def finished(self, t: float) -> bool:
        if self.loop or self.ping_pong:
            return False
        return t >= self.total_duration

    # --- between takes --------------------------------------------------------
    #
    # What actually happens on a set: back to one, the director asks for an
    # adjustment, the operator applies it, and they go again. The adjustment is
    # almost never "reprogram the move" -- it is "same move, slower" or "same
    # move, favour her a bit". A rig that can only be reprogrammed is a rig
    # that gets left in the case.

    def segment(self, first: int, last: int) -> "Move":
        """Just part of the move, as a move in its own right.

        "Again from node 3" is how blocking actually works: you rehearse the
        difficult third of a shot twenty times and the easy first third once.
        Replaying the whole move to reach the part being worked on wastes the
        crew's time and the battery.

        The first node of a segment keeps its position but loses its dwell --
        a hold that made sense mid-move is dead air at the top of a rehearsal.
        """
        n = len(self.waypoints)
        if n < 2:
            raise ValueError("nothing to take a segment of")
        if not (0 <= first < n and 0 <= last < n):
            raise ValueError(f"nodes must be within 0..{n - 1}")
        if first == last:
            raise ValueError("a segment needs two different nodes")
        lo, hi = (first, last) if first < last else (last, first)

        out = Move.from_dict(self.to_dict())
        out.name = f"{self.name} [{lo + 1}-{hi + 1}]"
        out.waypoints = out.waypoints[lo:hi + 1]
        out.waypoints[0].dwell = 0.0
        # A segment of a longer move is a rehearsal, not the shot; looping the
        # whole shot and looping a fragment of it are different intentions and
        # inheriting the flag surprises people.
        out.loop = False
        out.ping_pong = False
        return out

    def retimed(self, factor: float | None = None,
                total: float | None = None) -> "Move":
        """The same shape, at a different speed.

        Scales every leg and every dwell by the same factor, so the move keeps
        its rhythm: a beat that was a fifth of the move stays a fifth of it.
        Scaling only the legs and leaving the dwells alone is the tempting
        shortcut and it changes the timing of the shot, not just its length.

        Give a factor, or a target total duration and let it work the factor
        out -- "make it eight seconds" is how the request actually arrives.
        """
        if (factor is None) == (total is None):
            raise ValueError("give exactly one of factor or total")
        if total is not None:
            if total <= 0:
                raise ValueError("total duration must be positive")
            current = self.total_duration
            if current <= 0:
                raise ValueError("this move has no duration to scale")
            factor = total / current
        if factor <= 0:
            raise ValueError("retime factor must be positive")

        out = Move.from_dict(self.to_dict())
        for w in out.waypoints:
            # MIN_LEG guards the sampler, which divides by duration.
            w.duration = max(MIN_LEG_S, w.duration * factor)
            w.dwell = w.dwell * factor
        return out

    def offset(self, pitch: float = 0.0, yaw: float = 0.0) -> "Move":
        """Shift the whole move. The director's "favour her a bit"."""
        out = Move.from_dict(self.to_dict())
        for w in out.waypoints:
            w.pitch = wrap180(w.pitch + pitch)
            w.yaw = wrap180(w.yaw + yaw)
        return out

    def referenced_to(self, pitch: float, yaw: float) -> "Move":
        """Move the whole shot so it starts from where the head is NOW.

        The routine after a remount, a battery swap, or someone nudging the
        tripod: aim at the frame you want as the first node and re-reference.
        Without this a stored move is only usable from the exact rigging it
        was authored on, which on a real set it never is.
        """
        if not self.waypoints:
            raise ValueError("nothing to reference")
        first = self.waypoints[0]
        return self.offset(pitch=wrap180(pitch - first.pitch),
                           yaw=wrap180(yaw - first.yaw))

    def start_error(self, pitch: float, yaw: float) -> tuple[float, float]:
        """How far the head is from where this move begins, per axis."""
        if not self.waypoints:
            return (0.0, 0.0)
        first = self.waypoints[0]
        return (wrap180(pitch - first.pitch), wrap180(yaw - first.yaw))

    def at_start(self, pitch: float, yaw: float,
                 tolerance: float = START_TOLERANCE_DEG) -> bool:
        """Is the head parked at the top of this move?

        The guard against the bumped tripod. Somebody will knock it between
        takes and nobody will admit it; the move then runs perfectly from the
        wrong place, which looks like a working rig producing an unusable
        take.
        """
        dp, dy = self.start_error(pitch, yaw)
        return abs(dp) <= tolerance and abs(dy) <= tolerance

    # --- setup fingerprint ---------------------------------------------------

    SETUP_FIELDS = ("mount", "height_cm", "base_orientation", "route",
                    "lens_accessory", "nd_filter", "zoom", "fov_deg", "notes")

    def fov_deg(self) -> float | None:
        """Horizontal field of view, if the operator has stated it.

        Every pixel figure this rig produces -- how closely two takes match,
        how much a drift shows -- depends on the lens, and nothing on this
        camera reports it. Stated once here, it travels with the shot and is
        used everywhere instead of a house assumption. Absent, the assumption
        is used and labelled as one.
        """
        raw = self.setup.get("fov_deg")
        if raw in (None, ""):
            return None
        try:
            got = float(raw)
        except (TypeError, ValueError):
            return None
        return got if 1.0 <= got <= 180.0 else None

    def setup_summary(self) -> str:
        parts = [f"{k}={self.setup[k]}" for k in self.SETUP_FIELDS
                 if self.setup.get(k) not in (None, "")]
        return "; ".join(parts)

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "loop": self.loop,
            "ping_pong": self.ping_pong,
            "route_arcs": self.route_arcs,
            "setup": dict(self.setup),
            "waypoints": [w.to_dict() for w in self.waypoints],
        }

    @staticmethod
    def from_dict(d: dict) -> "Move":
        return Move(
            name=str(d.get("name", "untitled")),
            loop=_bool_field(d, "loop", False),
            ping_pong=_bool_field(d, "ping_pong", False),
            # Defaults on for a file that predates the flag: an old move that
            # crossed the wedge was already playing wrong, and reproducing the
            # wrong path faithfully is not a kindness.
            route_arcs=_bool_field(d, "route_arcs", True),
            setup=dict(d.get("setup") or {}),
            waypoints=[Waypoint.from_dict(w) for w in d.get("waypoints", [])],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @staticmethod
    def from_json(text: str) -> "Move":
        return Move.from_dict(json.loads(text))


# --- move presets -----------------------------------------------------------
# The eMotimo ST4 ships 11 named move characters, from "quiet interview" to
# "turbo". Speed and easing together are what give a move its character, so
# they are named as pairs rather than left as two loose numbers.

PRESETS = {
    "interview":  {"duration": 20.0, "easing": "ease-in-out-sine"},
    "slow-reveal": {"duration": 12.0, "easing": "ease-in-out-sine"},
    "reframe":    {"duration": 4.0, "easing": "ease-in-out"},
    "punch-in":   {"duration": 1.5, "easing": "ease-in-out-cubic"},
    "whip":       {"duration": 0.5, "easing": "ease-in"},
    "drift":      {"duration": 45.0, "easing": "linear"},
}
