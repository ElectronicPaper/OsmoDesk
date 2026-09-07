"""A census of everything the camera says that we do not yet understand.

The driver subscribes to five camera status streams -- cam_status, cam_storage,
cam_expo_param, cam_video_param_v2, cam_record_time -- and decodes none of
them. Two opcodes are parsed in total: gimbal attitude and power. Everything
else arrives and is dropped on the floor.

That is why the monitor cannot prove the camera is recording, cannot say how
much card is left, and reports exposure as "what we asked for" rather than
what the camera is doing. Not because the data is unavailable: because nobody
has read it.

This is the instrument for reading it. It watches the inbound frames and, for
each opcode, records how often it arrives, what lengths it comes in, and --
the useful part -- WHICH BYTES EVER CHANGE.

Byte volatility is what makes an unknown payload tractable. Constant bytes are
structure. A byte that flips between exactly two values while the operator
starts and stops recording is the record flag. A byte that climbs steadily is
a counter or a clock; four that climb together are a little-endian one. You do
not have to guess the format, you watch it move.

Nothing here sends anything. It is a passive observer on a callback the
datalink already offers and nothing else uses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Opcodes this driver already understands. Listed so the census can say what
# is genuinely unread rather than burying it among the things that are fine.
KNOWN = {
    (0x04, 0x05): "gimbal attitude",
    (0x0D, 0x02): "power status",
}

# Payload bytes to track per opcode. Camera status pushes are short; a cap
# keeps a rogue long frame from making the report unreadable.
MAX_TRACKED_BYTES = 64

# Samples kept per opcode: the first, and up to this many that differ from it.
MAX_SAMPLES = 4


@dataclass
class OpcodeRecord:
    cmd_set: int
    cmd_id: int
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    lengths: set[int] = field(default_factory=set)
    samples: list[bytes] = field(default_factory=list)
    # Per byte offset: the distinct values seen there. Bounded, because a
    # counter byte would otherwise accumulate 256 entries and say nothing a
    # simple "it varies" does not.
    _values: list[set[int]] = field(default_factory=list)
    _overflowed: set[int] = field(default_factory=set)

    @property
    def opcode(self) -> str:
        return f"{self.cmd_set:#04x}/{self.cmd_id:#04x}"

    @property
    def known(self) -> str | None:
        return KNOWN.get((self.cmd_set, self.cmd_id))

    def volatile_bytes(self) -> list[int]:
        """Offsets whose value has ever changed. Where the meaning lives."""
        return [i for i, seen in enumerate(self._values)
                if len(seen) > 1 or i in self._overflowed]

    def constant_bytes(self) -> dict[int, int]:
        """Offsets that have never changed, and their value. Structure."""
        return {i: next(iter(seen)) for i, seen in enumerate(self._values)
                if len(seen) == 1 and i not in self._overflowed}

    def to_dict(self) -> dict:
        volatile = self.volatile_bytes()
        return {
            "opcode": self.opcode,
            "cmd_set": self.cmd_set,
            "cmd_id": self.cmd_id,
            "known": self.known,
            "count": self.count,
            "lengths": sorted(self.lengths),
            "volatile_bytes": volatile,
            "constant_bytes": {str(k): v for k, v in self.constant_bytes().items()},
            "samples": [s.hex(" ") for s in self.samples],
            "first_seen": round(self.first_seen, 2),
            "last_seen": round(self.last_seen, 2),
        }


class FrameCensus:
    """Passive record of every opcode seen, and which of its bytes move."""

    def __init__(self, max_opcodes: int = 128):
        self.records: dict[tuple[int, int], OpcodeRecord] = {}
        self.max_opcodes = max_opcodes
        self.started = time.monotonic()
        self.dropped_opcodes = 0
        # Marks let an operator segment the capture: press record, mark
        # "recording", stop, mark "stopped". Comparing the two segments is
        # what identifies the record flag.
        self.marks: list[tuple[float, str]] = []

    def mark(self, label: str) -> None:
        """Timestamp a note in the capture, e.g. 'record started'."""
        self.marks.append((round(time.monotonic() - self.started, 2), str(label)[:60]))

    def note(self, frame) -> None:
        """Record one inbound frame. Must not raise: this runs on the read
        thread, and an exception here would take the datalink down."""
        try:
            key = (int(frame.cmd_set), int(frame.cmd_id))
            payload = bytes(frame.payload or b"")
        except Exception:
            return

        rec = self.records.get(key)
        if rec is None:
            if len(self.records) >= self.max_opcodes:
                self.dropped_opcodes += 1
                return
            rec = OpcodeRecord(cmd_set=key[0], cmd_id=key[1],
                               first_seen=time.monotonic() - self.started)
            self.records[key] = rec

        now = time.monotonic() - self.started
        rec.count += 1
        rec.last_seen = now
        rec.lengths.add(len(payload))

        tracked = payload[:MAX_TRACKED_BYTES]
        first_frame = rec.count == 1
        while len(rec._values) < len(tracked):
            # A byte first seen on a LATER, longer frame has not been constant
            # for the earlier short ones -- it was absent, and reporting it as
            # structure would be a lie about a field that may not exist in the
            # short form. On the first frame there is no "earlier", so its
            # bytes are ordinary candidates for being constant.
            if not first_frame:
                rec._overflowed.add(len(rec._values))
            rec._values.append(set())
        for i, b in enumerate(tracked):
            seen = rec._values[i]
            if len(seen) < 8:
                seen.add(b)
            elif b not in seen:
                rec._overflowed.add(i)

        if not rec.samples:
            rec.samples.append(tracked)
        elif len(rec.samples) < MAX_SAMPLES and tracked not in rec.samples:
            rec.samples.append(tracked)

    def report(self, unknown_only: bool = False) -> dict:
        """Everything seen, busiest first."""
        rows = [r for r in self.records.values()
                if not (unknown_only and r.known)]
        rows.sort(key=lambda r: r.count, reverse=True)
        return {
            "elapsed": round(time.monotonic() - self.started, 1),
            "opcodes": len(self.records),
            "frames": sum(r.count for r in self.records.values()),
            "dropped_opcodes": self.dropped_opcodes,
            "marks": [{"at": t, "label": l} for t, l in self.marks],
            "rows": [r.to_dict() for r in rows],
        }

    def reset(self) -> None:
        self.records.clear()
        self.marks.clear()
        self.dropped_opcodes = 0
        self.started = time.monotonic()
