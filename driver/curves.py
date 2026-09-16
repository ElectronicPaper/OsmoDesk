"""Bounded, monotone cubic timing curves. Pure math; no camera access.

The x coordinate is input progress, not the Bezier parameter. Inverting it
before evaluating y is essential for both motion and derivative correctness.
"""
from __future__ import annotations

import math


def validate_curve(value):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) for v in value)):
        raise ValueError("axis curve must contain four finite numbers")
    x1, y1, x2, y2 = map(float, value)
    if not (.02 <= x1 <= x2 <= .98 and 0 <= y1 <= y2 <= 1):
        raise ValueError("curve handles require .02 <= x1 <= x2 <= .98 and 0 <= y1 <= y2 <= 1")
    return [x1, y1, x2, y2]


def _coordinate(u, a, b):
    v = 1 - u
    return 3 * v * v * u * a + 3 * v * u * u * b + u ** 3


def _derivatives(u, a, b):
    first = 3 * ((1-u)**2 * a + 2*(1-u)*u*(b-a) + u*u*(1-b))
    second = 6 * ((1-u)*(b-2*a) + u*(1-2*b+a))
    return first, second


def evaluate(curve, s):
    """Return y(x), dy/dx, d2y/dx2 for a validated curve (None is linear)."""
    s = max(0.0, min(1.0, s))
    if curve is None:
        return s, 1.0, 0.0
    x1, y1, x2, y2 = curve
    if s in (0, 1):
        u = s
    else:
        lo, hi = 0.0, 1.0
        for _ in range(40):
            u = (lo + hi) * .5
            if _coordinate(u, x1, x2) < s:
                lo = u
            else:
                hi = u
        u = (lo + hi) * .5
    dx, ddx = _derivatives(u, x1, x2)
    dy, ddy = _derivatives(u, y1, y2)
    return _coordinate(u, y1, y2), dy / dx, (ddy * dx - dy * ddx) / dx**3


def slope_bound(curve):
    """Maximum dy/dx, including interior extrema; never a sampled peak.

    Both derivatives are quadratics. Their ratio's stationary equation is
    quadratic too (the cubic terms cancel). Linked axes use the product of
    their individual bounds, conservative even if the peaks do not coincide.
    """
    if curve is None:
        return 1.0
    x1, y1, x2, y2 = curve
    ax, bx, cx = 3*(1-3*x2+3*x1), 6*(x2-2*x1), 3*x1
    ay, by, cy = 3*(1-3*y2+3*y1), 6*(y2-2*y1), 3*y1
    a, b, c = ay*bx-by*ax, 2*(ay*cx-cy*ax), by*cx-cy*bx
    candidates = [0.0, 1.0]
    if abs(a) < 1e-12:
        if abs(b) > 1e-12:
            candidates.append(-c/b)
    else:
        discriminant = b*b - 4*a*c
        if discriminant >= 0:
            root = math.sqrt(discriminant)
            candidates.extend(((-b-root)/(2*a), (-b+root)/(2*a)))
    return max(((ay*u+by)*u+cy) / ((ax*u+bx)*u+cx)
               for u in candidates if 0 <= u <= 1) * (1 + 1e-12)


def easing_kinematics(name, s):
    """Existing easing value and analytic derivatives on normalized time."""
    s = max(0., min(1., s))
    if name == "linear":
        return s, 1., 0.
    if name == "ease-in":
        return s*s, 2*s, 2.
    if name == "ease-out":
        return 1-(1-s)**2, 2*(1-s), -2.
    if name == "ease-in-out":
        return (2*s*s, 4*s, 4.) if s < .5 else (1-2*(1-s)**2, 4*(1-s), -4.)
    if name == "ease-in-out-cubic":
        return (4*s**3, 12*s*s, 24*s) if s < .5 else (1-4*(1-s)**3, 12*(1-s)**2, -24*(1-s))
    if name == "ease-in-out-sine":
        return (1-math.cos(math.pi*s))*.5, math.pi*.5*math.sin(math.pi*s), math.pi**2*.5*math.cos(math.pi*s)
    raise ValueError("unknown easing: " + str(name))


EASING_SLOPES = {"linear": 1., "ease-in": 2., "ease-out": 2.,
                 "ease-in-out": 2., "ease-in-out-cubic": 3.,
                 "ease-in-out-sine": math.pi/2}


def compose(outer, inner):
    """Chain rule for a follower curve evaluated at leader progress."""
    value, first, second = evaluate(outer, inner[0])
    return value, first*inner[1], second*inner[1]**2 + first*inner[2]
