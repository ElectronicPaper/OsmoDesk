"""Cube LUT parsing.

A .cube file is the interchange format every grading tool writes, and it is
the only piece of a monitor LUT pipeline that fails quietly. A malformed file
does not raise -- it produces a lookup table that is subtly wrong, and the
operator grades against it for an hour before noticing the shadows are green.
So the parsing happens here, in Python, where it can be tested, rather than in
the browser where it cannot.

The viewer only ever receives a table this module has already validated: right
number of entries, finite values, a sane domain. Anything else is rejected
with a message that names the line.

Format, per Adobe's specification:

    TITLE "some look"          optional
    LUT_3D_SIZE 33             or LUT_1D_SIZE
    DOMAIN_MIN 0.0 0.0 0.0     optional, defaults to 0
    DOMAIN_MAX 1.0 1.0 1.0     optional, defaults to 1
    0.0 0.0 0.0                size**3 triples for 3D, size for 1D
    ...

Red varies fastest in the 3D table, then green, then blue. Getting that order
wrong transposes the cube and swaps the colour axes, which looks like a look
rather than like a bug -- another reason this is tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# A 3D LUT is size**3 * 3 floats. 64 is the largest anyone ships (Resolve
# writes 33, most cameras 17 or 33); 65**3 would be 2.7M floats through a JSON
# response, which is not a monitor feature any more.
MAX_3D_SIZE = 64
MIN_SIZE = 2
MAX_1D_SIZE = 65536


class LutError(ValueError):
    """A .cube file that cannot be trusted. The message names the problem."""


@dataclass(frozen=True)
class Lut:
    """A parsed lookup table, ready to hand to a shader.

    `data` is flat and interleaved RGB, in the file's own order: for a 3D LUT
    that is red-fastest, which is also the order a WebGL 3D texture wants, so
    no transposition happens anywhere between here and the GPU.
    """

    title: str
    size: int
    dimensions: int          # 1 or 3
    data: list[float]
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0)

    @property
    def entries(self) -> int:
        return self.size ** 3 if self.dimensions == 3 else self.size

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "size": self.size,
            "dimensions": self.dimensions,
            "domain_min": list(self.domain_min),
            "domain_max": list(self.domain_max),
            "data": self.data,
        }

    def sample_nearest(self, r: float, g: float, b: float) -> tuple[float, float, float]:
        """Nearest-neighbour lookup. For tests and sanity checks, not for the
        viewer -- the shader interpolates."""
        if self.dimensions == 1:
            i = _index_for(r, self.domain_min[0], self.domain_max[0], self.size)
            return (self.data[i * 3], self.data[i * 3 + 1], self.data[i * 3 + 2])
        ri = _index_for(r, self.domain_min[0], self.domain_max[0], self.size)
        gi = _index_for(g, self.domain_min[1], self.domain_max[1], self.size)
        bi = _index_for(b, self.domain_min[2], self.domain_max[2], self.size)
        # Red fastest, then green, then blue.
        i = (bi * self.size * self.size + gi * self.size + ri) * 3
        return (self.data[i], self.data[i + 1], self.data[i + 2])


def _index_for(v: float, lo: float, hi: float, size: int) -> int:
    span = hi - lo
    s = 0.0 if span == 0 else (v - lo) / span
    return max(0, min(size - 1, int(round(s * (size - 1)))))


def _floats(parts: list[str], line_no: int, want: int, what: str) -> list[float]:
    if len(parts) != want:
        raise LutError(f"line {line_no}: {what} needs {want} numbers, got {len(parts)}")
    out = []
    for p in parts:
        try:
            f = float(p)
        except ValueError:
            raise LutError(f"line {line_no}: {p!r} is not a number") from None
        if not math.isfinite(f):
            raise LutError(f"line {line_no}: {p!r} is not finite")
        out.append(f)
    return out


def parse_cube(text: str) -> Lut:
    """Parse a .cube file, or raise LutError naming the line that broke it."""
    title = ""
    size = 0
    dimensions = 0
    dmin = [0.0, 0.0, 0.0]
    dmax = [1.0, 1.0, 1.0]
    data: list[float] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        key = parts[0].upper()

        if key == "TITLE":
            # Quoted, and may contain spaces.
            title = line[len(parts[0]):].strip().strip('"')
        elif key in ("LUT_3D_SIZE", "LUT_1D_SIZE"):
            if dimensions:
                raise LutError(f"line {line_no}: the file declares its size twice")
            want3 = key == "LUT_3D_SIZE"
            try:
                size = int(parts[1])
            except (IndexError, ValueError):
                raise LutError(f"line {line_no}: {key} needs a whole number") from None
            cap = MAX_3D_SIZE if want3 else MAX_1D_SIZE
            if not (MIN_SIZE <= size <= cap):
                raise LutError(
                    f"line {line_no}: {key} of {size} is outside {MIN_SIZE}..{cap}")
            dimensions = 3 if want3 else 1
        elif key == "DOMAIN_MIN":
            dmin = _floats(parts[1:], line_no, 3, "DOMAIN_MIN")
        elif key == "DOMAIN_MAX":
            dmax = _floats(parts[1:], line_no, 3, "DOMAIN_MAX")
        elif key.startswith("LUT_"):
            # An unknown LUT_ directive means a dialect this parser does not
            # understand. Better to refuse than to guess at a colour pipeline.
            raise LutError(f"line {line_no}: unsupported directive {parts[0]!r}")
        else:
            data.extend(_floats(parts, line_no, 3, "a table row"))

    if not dimensions:
        raise LutError("no LUT_3D_SIZE or LUT_1D_SIZE -- not a .cube file")

    for i, (lo, hi) in enumerate(zip(dmin, dmax)):
        if hi <= lo:
            raise LutError(
                f"DOMAIN_MAX[{i}] ({hi}) must be greater than DOMAIN_MIN[{i}] ({lo})")

    want = (size ** 3 if dimensions == 3 else size) * 3
    if len(data) != want:
        raise LutError(
            f"expected {want // 3} table rows for a {dimensions}D LUT of size "
            f"{size}, found {len(data) // 3}")

    return Lut(title=title, size=size, dimensions=dimensions, data=data,
               domain_min=(dmin[0], dmin[1], dmin[2]),
               domain_max=(dmax[0], dmax[1], dmax[2]))


def identity(size: int = 17) -> Lut:
    """A LUT that changes nothing. The reference every other LUT is judged
    against, and what the viewer falls back to when a look is cleared."""
    if not (MIN_SIZE <= size <= MAX_3D_SIZE):
        raise LutError(f"identity size {size} is outside {MIN_SIZE}..{MAX_3D_SIZE}")
    data: list[float] = []
    last = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
                data.extend((r / last, g / last, b / last))
    return Lut(title="identity", size=size, dimensions=3, data=data)
