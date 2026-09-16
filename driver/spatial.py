"""Pure, bounded angular screen-space rehearsal helpers.

These helpers deliberately use only authored gimbal angles and measured angle
traces.  They do not know where a subject is in three-dimensional space, nor
do they make a claim about a lens, roll, zoom, translation, or physical rig.
"""

from __future__ import annotations

import math
import bisect
from collections.abc import Sequence
from numbers import Real

from . import director, moves, response

MAX_MARKERS = 24
MAX_SAMPLES = 1200
MAX_TRACE_POINTS = 20_000
MAX_DURATION = 3600.0
MAX_ANALYSIS_DURATION = director.MAX_TARGET_DURATION
SETTLE_TOLERANCE_DEG = 0.25
SETTLE_WINDOW_SECONDS = 0.25
MAX_GAP_SECONDS = 1.0
MAX_SHAPE_RATE = 4.0

SCOPE = ("sampled rotation-only gimbal geometry; no depth, roll, lens, zoom, "
         "translation, scene, or physical-stability inference")


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be a finite number")
    return out


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _options(options: object) -> tuple[float, float, float, tuple[float, float], list[dict], float | None]:
    data = _object(options, "options")
    allowed = {"hfov", "aspect", "margin", "origin", "markers", "target_duration"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("unknown spatial option")
    hfov = _number(data.get("hfov", 84.0), "hfov")
    aspect = _number(data.get("aspect", 16 / 9), "aspect")
    margin = _number(data.get("margin", 0.1), "margin")
    if not 1.0 <= hfov <= 150.0:
        raise ValueError("hfov must be within 1..150 degrees")
    if not 0.3 <= aspect <= 4.0:
        raise ValueError("aspect must be within 0.3..4")
    if not 0.0 <= margin <= 0.4:
        raise ValueError("margin must be within 0..0.4")
    origin = _object(data.get("origin", {"pitch": 0.0, "yaw": 0.0}), "origin")
    if set(origin) - {"pitch", "yaw"} or "pitch" not in origin or "yaw" not in origin:
        raise ValueError("origin must contain only pitch and yaw")
    origin_pair = (_number(origin["pitch"], "origin pitch"),
                   _number(origin["yaw"], "origin yaw"))
    raw_markers = data.get("markers", [])
    if not isinstance(raw_markers, list) or len(raw_markers) > MAX_MARKERS:
        raise ValueError(f"markers must be a list of at most {MAX_MARKERS}")
    markers: list[dict] = []
    for i, raw in enumerate(raw_markers):
        marker = _object(raw, f"marker {i}")
        allowed_marker = {"name", "pitch", "yaw", "kind", "not_before", "hold"}
        if set(marker) - allowed_marker:
            raise ValueError(f"marker {i} has an unknown field")
        if not isinstance(marker.get("name"), str) or not marker["name"] or len(marker["name"]) > 256:
            raise ValueError(f"marker {i} name must be nonempty text")
        kind = marker.get("kind")
        if kind not in ("subject", "avoid"):
            raise ValueError(f"marker {i} kind must be subject or avoid")
        not_before = marker.get("not_before")
        if not_before is not None:
            not_before = _number(not_before, f"marker {i} not_before")
            if not_before < 0:
                raise ValueError(f"marker {i} not_before must be nonnegative")
        hold = marker.get("hold")
        if hold is not None:
            hold = _number(hold, f"marker {i} hold")
            if hold < 0 or hold > MAX_ANALYSIS_DURATION:
                raise ValueError(f"marker {i} hold must be within 0..{MAX_ANALYSIS_DURATION:g}")
        markers.append({"name": marker["name"], "pitch": _number(marker.get("pitch"), f"marker {i} pitch"),
                        "yaw": _number(marker.get("yaw"), f"marker {i} yaw"), "kind": kind,
                        "not_before": not_before, "hold": hold})
    target = data.get("target_duration")
    if target is not None:
        target = _number(target, "target_duration")
        if not 0.1 <= target <= MAX_ANALYSIS_DURATION:
            raise ValueError(f"target_duration must be within 0.1..{MAX_ANALYSIS_DURATION:g}")
    return hfov, aspect, margin, origin_pair, markers, target


def _world(pitch: float, yaw: float) -> tuple[float, float, float]:
    p, y = math.radians(pitch), math.radians(yaw)
    return (math.sin(y) * math.cos(p), math.sin(p), -math.cos(y) * math.cos(p))


def _visible(camera_pitch: float, camera_yaw: float, marker: dict,
             tan_h: float, tan_v: float) -> bool:
    p, y = math.radians(camera_pitch), math.radians(camera_yaw)
    right = (math.cos(y), 0.0, math.sin(y))
    up = (-math.sin(y) * math.sin(p), math.cos(p), math.cos(y) * math.sin(p))
    forward = (math.sin(y) * math.cos(p), math.sin(p), -math.cos(y) * math.cos(p))
    wx, wy, wz = _world(marker["pitch"], marker["yaw"])
    x = right[0] * wx + right[1] * wy + right[2] * wz
    yy = up[0] * wx + up[1] * wy + up[2] * wz
    z = forward[0] * wx + forward[1] * wy + forward[2] * wz
    return z > 0 and abs(x / z) <= tan_h and abs(yy / z) <= tan_v


def _beat_times(move: moves.Move) -> set[float]:
    """Exact authored arrival/hold instants for one sampled cycle."""
    result: set[float] = set()
    arrivals = move.arrival_times()
    for arrival, waypoint in zip(arrivals, move.waypoints):
        result.add(arrival)
        result.add(arrival + waypoint.dwell)
    if move.ping_pong:
        cycle = move.cycle_duration
        for arrival, waypoint in zip(arrivals, move.waypoints):
            result.add(cycle - arrival)
            result.add(cycle - arrival - waypoint.dwell)
    return result


def _times(move: moves.Move) -> list[float]:
    span = move.cycle_duration
    if span <= 0:
        return []
    count = min(MAX_SAMPLES, max(2, int(math.ceil(span * 20.0)) + 1))
    result = {span * i / (count - 1) for i in range(count)}
    # Include beat instants from the move, not the rounded display timestamps
    # in Director's JSON output. Regular samples are approximate between them.
    for t in _beat_times(move):
        if 0.0 <= t <= span:
            result.add(t)
    return sorted(result)


def _display_samples(move: moves.Move) -> tuple[list[dict], bool]:
    """Bounded display points with no interpolation engine besides Move.sample.

    The timestamp set contains every beat and enough per-leg timestamps for a
    less-than-five-degree nominal angular hop.  It is a viewer aid only: points are
    returned in a continuous display turn to avoid a fake line across +/-180.
    """
    span = move.cycle_duration
    if span <= 0:
        return [], False
    # First retain every geometrically required time.  The regular clock is
    # deliberately added only after this set: reducing a long timeline must
    # never erase an authored beat or a required angular subdivision.
    required = _beat_times(move)
    arrivals = move.arrival_times()
    for index in range(1, len(move.waypoints)):
        start = arrivals[index - 1] + move.waypoints[index - 1].dwell
        end = arrivals[index]
        previous, waypoint = move.waypoints[index - 1], move.waypoints[index]
        pitch_arc = moves.PITCH_LIMITS if move.route_arcs else None
        yaw_arc = moves.YAW_LIMITS if move.route_arcs else None
        # This is the exact routing rule Move.sample uses.  Do not infer a
        # shortest path from endpoint samples: a legal route can exceed 180°.
        pitch_delta = moves.arc_delta(pitch_arc, previous.pitch, waypoint.pitch)
        yaw_delta = moves.arc_delta(yaw_arc, previous.yaw, waypoint.yaw)
        # Easing and the monotone flow spline can concentrate travel within a
        # fraction of a leg.  Four times the nominal leg rate is a conservative
        # display bound for the shipped shapes; it is not an actuator claim.
        rates = move.axis_rate_bounds(index-1) if waypoint.has_axis_curves else (MAX_SHAPE_RATE, MAX_SHAPE_RATE)
        subdivisions = max(1, int(math.ceil(max(rates[0]*abs(pitch_delta), rates[1]*abs(yaw_delta)) / 4.999)))
        if subdivisions * (2 if move.ping_pong else 1) + len(required) > 10_000:
            raise ValueError("required display beats and angular subdivisions exceed 10000 samples; soften curves")
        for step in range(1, subdivisions):
            t = start + (end - start) * step / subdivisions
            required.add(t)
            if move.ping_pong:
                required.add(span - t)
    if len(required) > 10_000:
        raise ValueError("required display beats and angular subdivisions exceed 10000 samples")

    # A regular clock is the normal viewer path.  It uses any remaining space
    # and reports when the requested 20 Hz visual density did not fit.
    regular_count = max(2, int(math.ceil(span * 20.0)) + 1)
    remaining = 10_000 - len(required)
    actual_regular_count = min(regular_count, max(0, remaining))
    resolution_limited = actual_regular_count < regular_count
    if actual_regular_count:
        for index in range(actual_regular_count):
            required.add(span * index / max(1, actual_regular_count - 1))
    rows: list[dict] = []
    last_raw = last_pitch = last_yaw = None
    for t in sorted(required)[:10_000]:
        raw = move.sample(t)
        if raw is None:
            continue
        pitch, yaw = raw
        if last_raw is not None:
            dp, dy = pitch - last_raw[0], yaw - last_raw[1]
            while dp > 180: dp -= 360
            while dp < -180: dp += 360
            while dy > 180: dy -= 360
            while dy < -180: dy += 360
            pitch, yaw = last_pitch + dp, last_yaw + dy
        rows.append({"time": t, "pitch": pitch, "yaw": yaw})
        last_raw, last_pitch, last_yaw = raw, pitch, yaw
    return rows, resolution_limited


def analyze(move: moves.Move, options: dict, max_dps: float = response.MAX_DPS) -> dict:
    """Return an unapplied Director preview plus bounded angular frame checks."""
    if not isinstance(move, moves.Move):
        raise ValueError("move must be a Move")
    hfov, aspect, margin, origin, markers, target = _options(options)
    max_dps = _number(max_dps, "max_dps")
    if max_dps <= 0:
        raise ValueError("max_dps must be positive")
    preview = director.preview(move, max_dps=max_dps, target_duration=target)
    times = _times(move)
    display_samples, resolution_limited = _display_samples(move)
    # Margin is a fraction on *each* edge, so 10% gives an 80% usable span.
    full_h = math.tan(math.radians(hfov) / 2.0)
    tan_h = full_h * (1.0 - 2.0 * margin)
    tan_v = tan_h / aspect
    sightings: list[list[bool]] = [[] for _ in markers]
    for t in times:
        point = move.sample(t)
        if point is None:
            continue
        pitch, yaw = point[0] + origin[0], point[1] + origin[1]
        for index, marker in enumerate(markers):
            # Subjects must fit the protected inner region. An unwanted object
            # at the delivery edge is still visible, even outside that margin.
            h, v = (full_h, full_h / aspect) if marker['kind'] == 'avoid' else (tan_h, tan_v)
            sightings[index].append(_visible(pitch, yaw, marker, h, v))
    checks = []
    for marker, visible_flags in zip(markers, sightings):
        interval_values: list[tuple[float, float]] = []
        start = None
        for t, shown in zip(times, visible_flags):
            if shown and start is None:
                start = t
            elif not shown and start is not None:
                interval_values.append((start, last_time))
                start = None
            last_time = t
        if start is not None:
            interval_values.append((start, times[-1]))
        visible = [t for t, shown in zip(times, visible_flags) if shown]
        first = visible[0] if visible else None
        last = visible[-1] if visible else None
        early = marker["not_before"] is not None and first is not None and first < marker["not_before"]
        held = True
        if marker["hold"] is not None:
            cutoff = marker["not_before"] or 0.0
            held = any(end - max(start, cutoff) >= marker["hold"]
                       for start, end in interval_values)
        # A named subject is itself an authored reveal intent.  An absent
        # subject is therefore a failed reveal even without an explicit hold.
        missing_intent = not visible and marker["kind"] == "subject"
        violates = bool(visible) if marker["kind"] == "avoid" else bool(early or not held or missing_intent)
        checks.append({"name": marker["name"], "kind": marker["kind"],
                       "intervals": [{"start": start, "end": end} for start, end in interval_values],
                       "first_visible": first, "last_visible": last,
                       "visible_seconds": sum(end - start for start, end in interval_values),
                       "violates_reveal": violates})
    horizontal = vertical = None
    vfov = math.degrees(2.0 * math.atan(math.tan(math.radians(hfov) / 2.0) / aspect))
    if len(display_samples) >= 2:
        rates = [(abs(moves.wrap180(b["yaw"] - a["yaw"])) / (b["time"] - a["time"]),
                  abs(moves.wrap180(b["pitch"] - a["pitch"])) / (b["time"] - a["time"]))
                 for a, b in zip(display_samples, display_samples[1:]) if b["time"] > a["time"]]
        if rates:
            horizontal = max(rate[0] / hfov for rate in rates)
            vertical = max(rate[1] / vfov for rate in rates)
    return {"preview": preview, "display_samples": display_samples,
            "display_resolution_limited": resolution_limited,
            "projection": {"markers": checks,
            "readability": {"horizontal_screen_widths_per_second": None if horizontal is None else round(horizontal, 6),
                            "vertical_frame_heights_per_second": None if vertical is None else round(vertical, 6),
                            "sample_count": len(display_samples), "approximate": True,
                            "scope": "sampled angular screen-rate heuristic; not a smoothness, flow, or jerk measurement"}, "scope": SCOPE}}


def recipe(move: moves.Move, template: dict) -> dict:
    """Build, but never apply, a timing/name treatment for existing points."""
    if not isinstance(move, moves.Move):
        raise ValueError("move must be a Move")
    data = _object(template, "template")
    roles = data.get("roles")
    if set(data) != {"roles"} or not isinstance(roles, list) or not 2 <= len(roles) <= MAX_MARKERS:
        raise ValueError("template must contain 2..24 roles")
    if len(roles) != len(move.waypoints):
        raise ValueError("template roles must exactly match existing points")
    out = move.to_dict()
    for i, role in enumerate(roles):
        role = _object(role, f"role {i}")
        if set(role) - {"name", "duration", "dwell", "easing"}:
            raise ValueError(f"role {i} has an unknown field")
        if set(role) != {"name", "duration", "dwell", "easing"}:
            raise ValueError(f"role {i} must define name, duration, dwell and easing")
        if not isinstance(role["name"], str) or not role["name"] or len(role["name"]) > 256:
            raise ValueError(f"role {i} name must be nonempty text")
        duration = _number(role["duration"], f"role {i} duration")
        dwell = _number(role["dwell"], f"role {i} dwell")
        if not 0.1 <= duration <= MAX_DURATION or not 0 <= dwell <= MAX_DURATION:
            raise ValueError(f"role {i} timing is outside the allowed range")
        if not isinstance(role["easing"], str):
            raise ValueError(f"role {i} easing must be text")
        moves.get_easing(role["easing"])
        point = out["waypoints"][i]
        point.update(name=role["name"], duration=duration, dwell=dwell, easing=role["easing"])
    return out


def settle(trace: list, arrivals: list) -> list:
    """Describe observed angular settling after named arrival instants.

    This is evidence about this finite trace only, never a physical stability
    claim. A missing or stale telemetry gap restarts the observation window.
    """
    if not isinstance(trace, list) or len(trace) > MAX_TRACE_POINTS:
        raise ValueError(f"trace must be a list of at most {MAX_TRACE_POINTS} points")
    if not isinstance(arrivals, list) or len(arrivals) > 200:
        raise ValueError("arrivals must be a list of at most 200 instants")
    points: list[tuple[float, float, float]] = []
    previous = None
    for i, point in enumerate(trace):
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) != 3:
            raise ValueError(f"trace point {i} must be [t, pitch, yaw]")
        t, pitch, yaw = (_number(value, f"trace point {i}") for value in point)
        if t < 0 or (previous is not None and t <= previous):
            raise ValueError("trace times must be nonnegative and strictly increasing")
        points.append((t, pitch, yaw)); previous = t
    arrival_values = [_number(value, f"arrival {i}") for i, value in enumerate(arrivals)]
    if any(value < 0 for value in arrival_values) or any(right <= left for left, right in zip(arrival_values, arrival_values[1:])):
        raise ValueError("arrivals must be nonnegative and strictly increasing")
    stamps = [point[0] for point in points]
    result = []
    for index, arrival in enumerate(arrival_values):
        end = arrival_values[index + 1] if index + 1 < len(arrival_values) else math.inf
        lo, hi = bisect.bisect_left(stamps, arrival), bisect.bisect_left(stamps, end)
        observed = points[lo:hi]
        settled_at = None
        sample_count = 0
        gap_observed = not observed or observed[0][0] - arrival > MAX_GAP_SECONDS
        # Unwrap both axes locally.  This keeps a +/-180 seam from looking like
        # a 360-degree wobble while still making no claim about physical rest.
        unwrapped: list[tuple[float, float, float]] = []
        for point in observed:
            if unwrapped and point[0] - unwrapped[-1][0] > MAX_GAP_SECONDS:
                gap_observed = True
                break
            if unwrapped:
                pitch = unwrapped[-1][1] + moves.wrap180(point[1] - observed[len(unwrapped) - 1][1])
                yaw = unwrapped[-1][2] + moves.wrap180(point[2] - observed[len(unwrapped) - 1][2])
            else:
                pitch, yaw = point[1], point[2]
            unwrapped.append((point[0], pitch, yaw))
        if not gap_observed:
            start = 0
            low_p = high_p = unwrapped[0][1]
            low_y = high_y = unwrapped[0][2]
            for current, point in enumerate(unwrapped):
                low_p, high_p = min(low_p, point[1]), max(high_p, point[1])
                low_y, high_y = min(low_y, point[2]), max(high_y, point[2])
                # A movement outside the tolerance begins a fresh candidate
                # window.  This is linear and conservative rather than an
                # expensive all-pairs stability search.
                if max(high_p - low_p, high_y - low_y) > SETTLE_TOLERANCE_DEG:
                    start = current
                    low_p = high_p = point[1]
                    low_y = high_y = point[2]
                if point[0] - unwrapped[start][0] >= SETTLE_WINDOW_SECONDS:
                    settled_at, sample_count = point[0], current - start + 1
                    break
        result.append({"arrival": arrival, "settled_at": settled_at,
                       "settling_seconds": None if settled_at is None else round(settled_at - arrival, 4),
                       "samples": sample_count, "gap_observed": gap_observed,
                       "approximate": True,
                       "scope": "measured angular trace observation only; not a physical stability claim"})
    return result
