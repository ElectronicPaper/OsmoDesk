"""Stick response linearisation.

The camera's own deflection-to-speed curve is unusable as a camera control.
Measured on an Osmo Pocket 4 Pro by holding each deflection for 1.4 s and
timing the reported angle, averaged over both directions and both axes:

    deflection   deg/s          deflection   deg/s
        0.05      0.07              0.55     14.6
        0.10      0.35              0.70     18.2
        0.15      0.85              0.85     21.0
        0.20      1.75              1.00     42.0
        0.30      4.40
        0.40      8.60

Three separate problems in one curve. Nothing useful happens below about 0.2,
so the first fifth of the stick is dead. Between 0.2 and 0.4 the speed
quadruples, so the next fifth is grabby. From 0.55 to 0.85 it barely changes at
all -- a plateau where pushing harder does nothing. Then full deflection
*doubles* the speed in one step, which lurches.

An operator cannot learn that. Worse, they cannot learn it *and* frame a shot,
which is the only reason to hold the stick at all.

So the curve is inverted here: callers ask for degrees per second and this
module works out the deflection that actually produces it. On top of the now
linear response sits a deliberate, gentle expo shape and a per-preset speed
cap, both chosen rather than inherited from the hardware.

Both axes measured the same to within noise -- 42.0 and 41.9 deg/s at full
deflection -- so one table serves both. An earlier note in gimbal.py claimed
yaw slewed three times faster than pitch; that was an artefact of comparing a
0.65 deflection against a 1.00 one, not a real difference.
"""

from __future__ import annotations

from bisect import bisect_left

# (deflection, deg/s). Monotonic in both columns, which the inverse lookup
# relies on. Measured, not modelled -- see the module docstring.
RESPONSE: list[tuple[float, float]] = [
    (0.00, 0.00),
    (0.10, 0.35),
    (0.15, 0.85),
    (0.20, 1.75),
    (0.30, 4.40),
    (0.40, 8.60),
    (0.55, 14.60),
    (0.70, 18.20),
    (0.85, 21.00),
    (1.00, 42.00),
]

MAX_DPS = RESPONSE[-1][1]

# Slowest speed the head will actually produce. Below this the deflection falls
# into the camera's own dead band, so a smaller request cannot be honoured and
# is treated as a stop rather than silently becoming one.
MIN_DPS = RESPONSE[1][1]

# Speed caps, in deg/s, for the three named sensitivities. These are what the
# presets should have been controlling all along: an operator can learn "fast
# is forty degrees a second", but not "fast multiplies a deflection whose
# meaning changes across its own range".
#
# 6 deg/s is a slow reveal; 18 is a normal following pan; 42 is a whip.
SPEED_CAPS = {"fine": 6.0, "normal": 18.0, "fast": MAX_DPS}

# Deliberate expo on the now-linear response. 1.0 would be pure linear, which
# gives no extra resolution around centre where framing adjustments live. 1.6
# roughly halves the sensitivity of the first third of throw while still
# reaching the cap at full deflection. Chosen, documented, and the same at
# every point -- unlike what the hardware does on its own.
EXPO = 1.6


def rate_for_input(value: float, cap: float = SPEED_CAPS["normal"]) -> float:
    """Operator input in -1..1 to a signed speed in deg/s."""
    v = max(-1.0, min(1.0, value))
    sign = 1.0 if v >= 0 else -1.0
    return sign * cap * (abs(v) ** EXPO)


def deflection_for_rate(dps: float) -> float:
    """Signed deg/s to the signed deflection that produces it.

    Piecewise-linear inverse of the measured table. Returns 0.0 for anything
    below the slowest speed the head can actually hold, because the deflection
    that would ask for it is inside the camera's dead band and produces
    nothing at all.
    """
    sign = 1.0 if dps >= 0 else -1.0
    want = abs(dps)
    if want < MIN_DPS:
        return 0.0
    if want >= MAX_DPS:
        return sign

    rates = [r for _, r in RESPONSE]
    i = bisect_left(rates, want)
    d0, r0 = RESPONSE[i - 1]
    d1, r1 = RESPONSE[i]
    if r1 == r0:                       # never true for this table; cheap guard
        return sign * d1
    return sign * (d0 + (d1 - d0) * (want - r0) / (r1 - r0))


def deflection_for_input(value: float,
                         cap: float = SPEED_CAPS["normal"]) -> float:
    """Operator input in -1..1 straight to the deflection to send.

    The whole correction in one call: apply the chosen expo and speed cap, then
    undo the camera's curve.
    """
    return deflection_for_rate(rate_for_input(value, cap))


def rate_for_deflection(deflection: float) -> float:
    """Forward lookup, for predicting what a raw deflection will do.

    Used by the travel-limit warning, which needs to know how fast the head is
    about to move before it has moved.
    """
    sign = 1.0 if deflection >= 0 else -1.0
    d = min(1.0, abs(deflection))
    defls = [x for x, _ in RESPONSE]
    i = bisect_left(defls, d)
    if i == 0:
        return 0.0
    d0, r0 = RESPONSE[i - 1]
    d1, r1 = RESPONSE[i]
    if d1 == d0:
        return sign * r1
    return sign * (r0 + (r1 - r0) * (d - d0) / (d1 - d0))
