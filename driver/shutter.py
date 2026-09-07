"""Shutter angle, and the two things that actually bite on set.

The camera takes a shutter speed as a denominator: 1/48, 1/50, 1/60. Cinema
does not think that way. A shutter is an ANGLE, because that is what it was on
a rotating disc, and the angle is what determines how the motion looks. The
conversion depends on frame rate:

    angle = 360 * fps / denominator

So 1/48 is 180 degrees at 24fps and 150 degrees at 20fps. The same shutter
speed is a different look at a different frame rate, which is exactly why an
operator sets the angle and lets the maths find the speed.

The other thing that bites is mains flicker. Any continuous light running off
an AC supply pulses at twice the mains frequency, and a shutter that is not a
whole multiple of that period samples a different part of the pulse each
frame. The result rolls through the shot as a brightness band. It is invisible
on a small monitor and obvious on a grade, so the safe speeds are computed
here rather than left to be remembered.
"""

from __future__ import annotations

import math

# Denominators an operator expects to find on a dial. The command itself takes
# any u16, so this is the controller's ladder, not a limit of the camera.
STANDARD = (
    24, 25, 30, 40, 48, 50, 60, 80, 100, 120, 160, 200, 240,
    320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 4000, 8000,
)

# The film convention. Half the frame period open, half closed.
CINE_ANGLE = 180.0

# Tolerance for calling a shutter flicker-safe, as a fraction of the period.
# A quarter of a percent is below what shows up in a grade and wide enough to
# accept 1/50 against a 50 Hz supply after floating point.
FLICKER_TOLERANCE = 0.0025


def angle_for(denominator: float, fps: float) -> float:
    """Shutter angle in degrees for a 1/`denominator` shutter at `fps`."""
    if denominator <= 0:
        raise ValueError("shutter denominator must be positive")
    if fps <= 0:
        raise ValueError("frame rate must be positive")
    return 360.0 * fps / denominator


def denominator_for_angle(angle: float, fps: float) -> float:
    """The exact shutter speed for an angle. Not rounded to the ladder."""
    if not (0.0 < angle <= 360.0):
        raise ValueError("shutter angle must be above 0 and at most 360 degrees")
    if fps <= 0:
        raise ValueError("frame rate must be positive")
    return 360.0 * fps / angle


def nearest_standard(denominator: float) -> int:
    """Snap to the ladder, choosing by ratio rather than by difference.

    An absolute difference makes every choice at the top of the ladder look
    identical -- 4000 and 8000 are 4000 apart, and so are 24 and 4024. Shutter
    is perceived in stops, so the nearest one in ratio is the nearest one to
    the eye.
    """
    if denominator <= 0:
        raise ValueError("shutter denominator must be positive")
    return min(STANDARD, key=lambda d: abs(math.log2(d / denominator)))


def angle_to_standard(angle: float, fps: float) -> int:
    """The ladder step closest to a requested angle at this frame rate."""
    return nearest_standard(denominator_for_angle(angle, fps))


def flicker_period_hz(mains_hz: float) -> float:
    """Light pulses at twice the supply frequency: both halves of the cycle
    deliver power."""
    if mains_hz <= 0:
        raise ValueError("mains frequency must be positive")
    return 2.0 * mains_hz


def flickers(denominator: float, mains_hz: float) -> bool:
    """True if this shutter will band under a light on that supply.

    Safe when the exposure is a whole number of half-cycles, so each frame
    integrates the same amount of the pulse.
    """
    if denominator <= 0:
        raise ValueError("shutter denominator must be positive")
    cycles = flicker_period_hz(mains_hz) / denominator
    nearest = round(cycles)
    if nearest < 1:
        # Faster than a single half-cycle: there is no whole number of pulses
        # to land on, so it always bands.
        return True
    return abs(cycles - nearest) > FLICKER_TOLERANCE


def flicker_safe(mains_hz: float, fastest: int = 8000) -> list[int]:
    """Every ladder step that will not band on that supply."""
    return [d for d in STANDARD if d <= fastest and not flickers(d, mains_hz)]


def describe(denominator: float, fps: float, mains_hz: float | None = None) -> dict:
    """Everything the UI needs for one shutter setting."""
    angle = angle_for(denominator, fps)
    out = {
        "denominator": denominator,
        "fps": fps,
        "angle": angle,
        "angle_label": (f"{angle:.0f}°" if abs(angle - round(angle)) < 0.05
                        else f"{angle:.1f}°"),
        "speed_label": f"1/{denominator:g}",
        "is_cine": abs(angle - CINE_ANGLE) < 0.5,
    }
    if mains_hz:
        out["flickers"] = flickers(denominator, mains_hz)
        out["mains_hz"] = mains_hz
    return out
