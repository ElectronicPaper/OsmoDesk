"""Pure proposal contracts for optional AI assistance; no execution authority."""

from __future__ import annotations

from copy import deepcopy
import math

from . import director, moves, response

MIN_BEATS = 2
MAX_BEATS = 24
MAX_BRIEF = 1600
MAX_TIME = 3600.0
BEAT_FIELDS = {"index", "name", "duration", "dwell", "easing"}
TREATMENT_FIELDS = {"title", "intent", "cautions", "beats"}


def _count(count: int) -> int:
    if type(count) is not int or not MIN_BEATS <= count <= MAX_BEATS:
        raise ValueError("Copilot requires 2 to 24 framing beats")
    return count


def _source(move: moves.Move) -> int:
    if not isinstance(move, moves.Move):
        raise ValueError("Copilot requires an authored move")
    return _count(len(move.waypoints))


def _text(value, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or not value.strip():
        raise ValueError(f"{field} must be nonempty text within {maximum} characters")
    return value.strip()


def _number(value, field: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a finite number within its bounds")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{field} must be a finite number within its bounds") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{field} must be a finite number within its bounds")
    return number


def _keys(value, expected: set[str], field: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{field} has missing or unsupported fields")


def build_context(move: moves.Move, brief: str, *, include_labels: bool = False) -> dict:
    """Return only intentionally shared text and relative, allowlisted motion facts."""
    _source(move)
    if type(include_labels) is not bool:
        raise ValueError("include_labels must be a boolean")
    if not isinstance(brief, str):
        raise ValueError("brief must be text")
    brief = _text(brief.strip(), "brief", MAX_BRIEF)
    beats = []
    for index, waypoint in enumerate(move.waypoints):
        previous = move.waypoints[max(0, index - 1)]
        distances = {}
        for attr, label, arc in (("yaw", "pan", moves.YAW_LIMITS),
                                 ("pitch", "tilt", moves.PITCH_LIMITS)):
            distance = abs(moves.arc_delta(arc if move.route_arcs else None,
                                          getattr(previous, attr), getattr(waypoint, attr)))
            distances[label] = _number(distance, "leg distance", 0, 360)
        if waypoint.easing not in moves.EASINGS:
            raise ValueError("Source beat has unsupported easing")
        beats.append({
            "index": index,
            "name": (_text(waypoint.name, "beat name", 64) if include_labels else f"P{index + 1}"),
            "duration": _number(waypoint.duration, "duration", moves.MIN_LEG_S, MAX_TIME),
            "dwell": _number(waypoint.dwell, "dwell", 0, MAX_TIME),
            "easing": waypoint.easing,
            "cue": waypoint.cue,
            "leg_distance_deg": distances,
        })
    return {"brief": brief, "loop": move.loop, "ping_pong": move.ping_pong,
            "cue_uncertain": move.has_cues, "allowed_easings": list(moves.EASINGS),
            "beats": beats}


def response_schema(count: int) -> dict:
    """Strict JSON schema; semantic order and no-op checks run locally as well."""
    count = _count(count)
    def obj(properties):
        return {"type": "object", "additionalProperties": False,
                "required": list(properties), "properties": properties}
    def string(maximum):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    beat = obj({
        "index": {"type": "integer", "minimum": 0, "maximum": count - 1},
        "name": string(64),
        "duration": {"type": "number", "minimum": moves.MIN_LEG_S, "maximum": MAX_TIME},
        "dwell": {"type": "number", "minimum": 0, "maximum": MAX_TIME},
        "easing": {"type": "string", "enum": list(moves.EASINGS)},
    })
    treatment = obj({
        "title": string(64), "intent": string(500),
        "cautions": {"type": "array", "maxItems": 4, "items": string(240)},
        "beats": {"type": "array", "minItems": count, "maxItems": count, "items": beat},
    })
    return obj({"treatments": {"type": "array", "minItems": 2, "maxItems": 2,
                               "items": treatment}})


def _pacing(waypoints: list[dict]) -> tuple:
    # A first position has no incoming leg; its easing and duration cannot alter pacing.
    return tuple((w["dwell"], w["duration"] if i else None, w["easing"] if i else None)
                 for i, w in enumerate(waypoints))


def validate_treatments(move: moves.Move, document: dict, *,
                        max_dps: float = response.MAX_DPS,
                        min_dps: float = response.MIN_DPS) -> list[dict]:
    """Validate untrusted proposals before constructing isolated canonical candidates."""
    count = _source(move)
    _keys(document, {"treatments"}, "response")
    treatments = document["treatments"]
    if not isinstance(treatments, list) or len(treatments) != 2:
        raise ValueError("Response must contain exactly two treatments")
    source = deepcopy(move.to_dict())
    original_pacing = _pacing(source["waypoints"])
    seen = set()
    results = []
    for treatment in treatments:
        _keys(treatment, TREATMENT_FIELDS, "treatment")
        title = _text(treatment["title"], "title", 64)
        intent = _text(treatment["intent"], "intent", 500)
        cautions = treatment["cautions"]
        if not isinstance(cautions, list) or len(cautions) > 4:
            raise ValueError("cautions must be a list of at most four entries")
        cautions = [_text(caution, "caution", 240) for caution in cautions]
        beats = treatment["beats"]
        if not isinstance(beats, list) or len(beats) != count:
            raise ValueError("Treatment must preserve the framing beat count")
        candidate_dict = deepcopy(source)
        diff = []
        for index, beat in enumerate(beats):
            _keys(beat, BEAT_FIELDS, "beat")
            if type(beat["index"]) is not int or beat["index"] != index:
                raise ValueError("Treatment must preserve framing beat indices and order")
            name = _text(beat["name"], "beat name", 64)
            duration = _number(beat["duration"], "duration", moves.MIN_LEG_S, MAX_TIME)
            dwell = _number(beat["dwell"], "dwell", 0, MAX_TIME)
            easing = beat["easing"]
            if not isinstance(easing, str) or easing not in moves.EASINGS:
                raise ValueError("Beat has unsupported easing")
            changes = {"name": name, "duration": duration, "dwell": dwell, "easing": easing}
            if index == 0:
                if (duration != source["waypoints"][0]["duration"]
                        or easing != source["waypoints"][0]["easing"]):
                    raise ValueError("First beat duration and easing must remain unchanged")
            for field, after in changes.items():
                before = candidate_dict["waypoints"][index][field]
                if before != after:
                    diff.append({"index": index, "field": field, "before": before, "after": after})
                    candidate_dict["waypoints"][index][field] = after
        pacing = _pacing(candidate_dict["waypoints"])
        if pacing == original_pacing:
            raise ValueError("Treatment must change shot pacing, not only labels")
        if pacing in seen:
            raise ValueError("Treatments must propose distinct shot pacing")
        seen.add(pacing)
        # Trusted original fields retain their exact values: no wrapping or clamping
        # of authored geometry, setup or first-position duration during conversion.
        candidate = moves.Move(**{**candidate_dict, "waypoints": [
            moves.Waypoint(**waypoint) for waypoint in candidate_dict["waypoints"]]})
        assessment = director.preview(candidate, max_dps=max_dps, min_dps=min_dps)
        results.append({"title": title, "intent": intent, "cautions": cautions,
                        "move": candidate.to_dict(), "diff": diff, "assessment": assessment})
    return results
