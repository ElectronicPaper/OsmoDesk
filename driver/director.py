"""Pure, bounded offline rehearsal and timing proposals for authored moves.

Director samples the canonical :class:`moves.Move`; it has no camera, thread,
storage or network dependency. Findings are sampled engineering checks, not a
physical certification of a gimbal, lens, mounting, scene or resulting shot.
"""

from __future__ import annotations

import math

from . import moves, preflight, response

MAX_VISUAL_SAMPLES = 600
VISUAL_HZ = 20.0
MAX_WAYPOINTS = 200
MAX_TARGET_DURATION = 24.0 * 60.0 * 60.0
SUGGESTION_ATTEMPTS = 8
SPEED_MARGIN = 1.02
SPAN_TOLERANCE_S = 1e-9


def _number(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        got = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(got):
        raise ValueError(f"{name} must be a finite number")
    return got


def _span(move: moves.Move) -> float:
    return move.cycle_duration


def _visual_samples(move: moves.Move) -> list[dict]:
    span = _span(move)
    if len(move.waypoints) < 2 or span <= 0:
        return []
    count = min(MAX_VISUAL_SAMPLES,
                max(2, int(math.ceil(span * VISUAL_HZ)) + 1))
    out = []
    for i in range(count):
        t = span * i / (count - 1)
        point = move.sample(t)
        if point is None:
            continue
        zoom = move.sample_zoom(t)
        out.append({
            "time": round(t, 4),
            "pitch": round(point[0], 6),
            "yaw": round(point[1], 6),
            "zoom": None if zoom is None else round(zoom, 6),
        })
    return out


def _beat_record(index: int, waypoint: moves.Waypoint, direction: str,
                 arrival: float, dwell_start: float,
                 dwell_end: float) -> dict:
    return {
        "index": index,
        "name": waypoint.name,
        "direction": direction,
        "arrival": round(arrival, 4),
        "dwell_start": round(dwell_start, 4),
        "dwell_end": round(dwell_end, 4),
        "cue": waypoint.cue,
    }


def _beats_and_events(move: moves.Move) -> tuple[list[dict], list[dict]]:
    arrivals = move.arrival_times()
    beats: list[dict] = []
    events: list[dict] = []
    cue_events: set[tuple[float, int]] = set()

    def add(index: int, direction: str, arrival: float,
            dwell_start: float, dwell_end: float) -> None:
        waypoint = move.waypoints[index]
        beats.append(_beat_record(index, waypoint, direction, arrival,
                                  dwell_start, dwell_end))
        base = {"beat_index": index, "name": waypoint.name,
                "direction": direction}
        events.append({**base, "time": round(arrival, 4), "type": "arrival"})
        if waypoint.dwell > 0:
            events.append({**base, "time": round(dwell_start, 4),
                           "type": "dwell_start"})
            events.append({**base, "time": round(dwell_end, 4),
                           "type": "dwell_end"})
        if waypoint.cue:
            cue_key = (round(arrival, 4), index)
            if cue_key not in cue_events:
                events.append({**base, "time": cue_key[0], "type": "cue"})
                cue_events.add(cue_key)

    for i, arrival in enumerate(arrivals):
        add(i, "forward", arrival, arrival,
            arrival + move.waypoints[i].dwell)

    if move.ping_pong:
        cycle = move.cycle_duration
        for i in range(len(move.waypoints) - 1, -1, -1):
            waypoint = move.waypoints[i]
            dwell_start = cycle - (arrivals[i] + waypoint.dwell)
            dwell_end = cycle - arrivals[i]
            add(i, "reverse", dwell_start, dwell_start, dwell_end)

    event_order = {"arrival": 0, "cue": 1, "dwell_start": 2, "dwell_end": 3}
    events.sort(key=lambda event: (event["time"], event_order[event["type"]],
                                   event["beat_index"]))
    beats.sort(key=lambda beat: (beat["arrival"], beat["index"]))
    return beats, events


def _retimed_for_span(move: moves.Move, duration: float) -> moves.Move:
    divisor = 2.0 if move.ping_pong else 1.0
    return move.retimed(total=duration / divisor)


def _report(move: moves.Move, max_dps: float,
            min_dps: float) -> preflight.Report:
    return preflight.check(move, max_dps=max_dps, min_dps=min_dps)


def _problem_reason(report: preflight.Report) -> str:
    kinds = {finding.kind for finding in report.findings}
    if "assessment" in kinds:
        return "move is outside the bounded offline assessment envelope"
    if "travel" in kinds:
        return "travel violations cannot be repaired by changing timing"
    if "too fast" in kinds and "dead band" in kinds:
        return "no checked duration satisfied both maximum speed and dead-band limits"
    if "too fast" in kinds:
        return "requested timing is too fast"
    if "dead band" in kinds:
        return "requested timing leaves motion below the dead band"
    return "no checked timing proposal is available"


def _suggest(candidate: moves.Move, report: preflight.Report,
             max_dps: float, min_dps: float) -> tuple[moves.Move, preflight.Report] | None:
    current = candidate
    current_report = report
    for _ in range(SUGGESTION_ATTEMPTS):
        fast = [abs(f.worst) / max_dps for f in current_report.findings
                if f.kind == "too fast"]
        slow = [abs(f.worst) / min_dps for f in current_report.findings
                if f.kind == "dead band"]
        lower = max(fast, default=0.0) * SPEED_MARGIN
        upper = min(slow, default=math.inf) / SPEED_MARGIN
        if fast and slow and lower > upper:
            return None
        if fast:
            factor = max(lower, 1.01)
        elif slow:
            factor = min(upper, 0.99)
        else:
            return None
        if not math.isfinite(factor) or factor <= 0:
            return None
        try:
            next_move = current.retimed(factor=factor)
        except ValueError:
            return None
        if (_span(next_move) > MAX_TARGET_DURATION
                or abs(_span(next_move) - _span(current)) < 1e-9):
            return None
        next_report = _report(next_move, max_dps, min_dps)
        if next_report.ok:
            return next_move, next_report
        if any(f.kind in ("travel", "assessment") for f in next_report.findings):
            return None
        current, current_report = next_move, next_report
    return None


def _proposal(move: moves.Move, original: preflight.Report,
              max_dps: float, min_dps: float,
              target_duration: float | None) -> dict:
    requested = target_duration
    candidate = move
    candidate_report = original
    if requested is not None:
        candidate = _retimed_for_span(move, requested)
        candidate_report = _report(candidate, max_dps, min_dps)
        if candidate_report.ok:
            assessed = _span(candidate)
            if not math.isclose(assessed, requested, rel_tol=1e-12,
                                abs_tol=SPAN_TOLERANCE_S):
                return {
                    "status": "suggested", "requested_duration": requested,
                    "assessed_duration": assessed,
                    "move": candidate.to_dict(),
                    "preflight": candidate_report.to_dict(),
                    "reason": (
                        "requested duration is shorter than the move's minimum "
                        "leg timing permits; returning the checked minimum-"
                        "timing alternative"
                    ),
                }
            return {
                "status": "requested_feasible", "requested_duration": requested,
                "assessed_duration": assessed, "move": candidate.to_dict(),
                "preflight": candidate_report.to_dict(), "reason": None,
            }
    elif candidate_report.ok:
        return {
            "status": "none", "requested_duration": None,
            "assessed_duration": _span(move), "move": None,
            "preflight": None, "reason": None,
        }

    if any(f.kind in ("travel", "assessment") for f in candidate_report.findings):
        return {
            "status": "impossible", "requested_duration": requested,
            "assessed_duration": _span(candidate), "move": None,
            "preflight": candidate_report.to_dict(),
            "reason": _problem_reason(candidate_report),
        }

    suggested = _suggest(candidate, candidate_report, max_dps, min_dps)
    if suggested is None:
        return {
            "status": "impossible", "requested_duration": requested,
            "assessed_duration": _span(candidate), "move": None,
            "preflight": candidate_report.to_dict(),
            "reason": _problem_reason(candidate_report),
        }
    suggested_move, suggested_report = suggested
    return {
        "status": "suggested", "requested_duration": requested,
        "assessed_duration": _span(suggested_move),
        "move": suggested_move.to_dict(),
        "preflight": suggested_report.to_dict(),
        "reason": _problem_reason(candidate_report),
    }


def preview(move: moves.Move, max_dps: float = response.MAX_DPS,
            min_dps: float = response.MIN_DPS,
            target_duration: float | None = None) -> dict:
    """Return a bounded canonical preview and a checked, unapplied proposal.

    ``target_duration`` means finite playback duration, or one complete cycle
    for a loop. A ping-pong target therefore retimes its forward half to half
    that value. Every returned candidate is produced solely by ``Move.retimed``.
    """
    if not isinstance(move, moves.Move):
        raise ValueError("move must be a Move")
    move.validate_axis_curves()
    if len(move.waypoints) > MAX_WAYPOINTS:
        raise ValueError(f"Director supports at most {MAX_WAYPOINTS} waypoints")
    try:
        authored_span = _number(_span(move), "move cycle duration")
    except ValueError:
        raise ValueError(
            "move cycle duration must be finite and within 0.."
            f"{MAX_TARGET_DURATION:g} seconds for Director rehearsal"
        ) from None
    if authored_span < 0 or authored_span > MAX_TARGET_DURATION:
        raise ValueError(
            "move cycle duration must be finite and within 0.."
            f"{MAX_TARGET_DURATION:g} seconds for Director rehearsal")
    max_dps = _number(max_dps, "max_dps")
    min_dps = _number(min_dps, "min_dps")
    if min_dps <= 0 or max_dps < min_dps:
        raise ValueError("speed interval must satisfy 0 < min_dps <= max_dps")
    if target_duration is not None:
        target_duration = _number(target_duration, "target_duration")
        if target_duration <= 0 or target_duration > MAX_TARGET_DURATION:
            raise ValueError(
                f"target_duration must be within 0..{MAX_TARGET_DURATION:g} seconds")

    report = _report(move, max_dps, min_dps)
    beats, events = _beats_and_events(move)
    return {
        "samples": _visual_samples(move),
        "beats": beats,
        "events": events,
        "timing": {
            "forward_duration": move.total_duration,
            "programmed_duration": move.playback_duration,
            "loop_uncertain": move.loop,
            "cue_uncertain": move.has_cues,
            "requested_duration": target_duration,
        },
        "preflight": report.to_dict(),
        "proposal": _proposal(move, report, max_dps, min_dps,
                              target_duration),
        "approximation": {
            "visual_sample_limit": MAX_VISUAL_SAMPLES,
            "preflight_sample_limit": preflight.MAX_SAMPLES,
            "leg_intervals": preflight.LEG_INTERVALS,
            "rehearsal_duration_limit": MAX_TARGET_DURATION,
            "target_duration_basis": "finite playback or one loop cycle",
            "scope": ("sampled arithmetic rehearsal only; not physical "
                      "certification or hardware acceptance"),
        },
    }
