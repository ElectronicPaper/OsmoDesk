"""Non-actuating axis graph data, derived from the canonical move model."""
from __future__ import annotations

import math
from . import moves, preflight, response


def preview(move: moves.Move, leg=1, max_dps=response.MAX_DPS,
            min_dps=response.MIN_DPS):
    if len(move.waypoints) > 200 or move.cycle_duration > 86400:
        raise ValueError("Curve preview supports at most 200 positions and 24 hours")
    move.validate_axis_curves()
    if isinstance(leg, bool) or not isinstance(leg, int) or not 1 <= leg < len(move.waypoints):
        raise ValueError("Choose a transition between two positions")
    start, end = move.waypoints[leg-1:leg+1]
    samples, warnings = [], []
    if move.uses_flow:
        # Exact derivatives of the existing shared Hermite path, not a second
        # motion engine. Manual handles remain disabled until Flow is removed.
        values = [move._unwrapped(axis) for axis in ("pitch", "yaw")]
        tangents = [move._tangents(axis) for axis in ("pitch", "yaw")]
        warnings.append("Flow uses shared pass-through tangents. Switch to manual curves to edit independent handles.")
    for index in range(241):
        s, row = index / 240, {"t": end.duration * index / 240}
        profiles = move.axis_progress(leg-1, s)
        for channel, axis in enumerate(("pitch", "yaw")):
            if move.uses_flow:
                a, b = values[channel][leg-1:leg+1]
                ma, mb = (v*end.duration for v in tangents[channel][leg-1:leg+1])
                c3, c2, c1 = 2*a-2*b+ma+mb, -3*a+3*b-2*ma-mb, ma
                position = ((c3*s+c2)*s+c1)*s+a
                speed = (3*c3*s*s+2*c2*s+c1)/end.duration
                accel = (6*c3*s+2*c2)/end.duration**2
            else:
                progress, first, second = profiles[channel]
                delta = moves.arc_delta(move._arc(axis), getattr(start, axis), getattr(end, axis))
                position = getattr(start, axis)+delta*progress
                speed, accel = delta*first/end.duration, delta*second/end.duration**2
            row.update({axis: position, axis+"_speed": speed, axis+"_accel": accel})
        if not all(math.isfinite(value) for value in row.values()):
            raise ValueError("Curve profile is not finite")
        samples.append(row)
    if end.axis_link != "independent":
        warnings.append("Linked axes follow planned progress, not live feedback from the other motor.")
    if not move.uses_flow:
        if any(abs(samples[j][axis+"_speed"]) > .01 for j in (0, -1) for axis in ("pitch", "yaw")):
            warnings.append("This transition has nonzero endpoint speed: a hold or a different neighbouring curve can cause a sudden speed change. Use Soft handles for a stopped keyframe.")
    warnings.append("Planned position, speed and acceleration; not measured camera motion. Motor acceleration limits are not calibrated. Holds and operator cue waits are outside this transition graph.")
    return {"leg": leg, "duration": end.duration, "samples": samples,
            "preflight": preflight.check(move, max_dps=max_dps, min_dps=min_dps).to_dict(),
            "warnings": warnings}
