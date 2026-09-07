"""Reassemble the Osmo live-view HEVC stream from datalink packets.

Ported from OpenPocketCine's `HevcDepacketizer.swift` and `Hevc.swift`
(Apache-2.0), which derived the framing from packet captures of DJI Mimo.

Video rides the same UDP 9004 socket as control, as `pktType 0x02` datagrams.
Each carries a fragment of one frame:

    byte 6      pktType, 0x02 for video
    byte 16     frame counter -- changes when a new frame starts
    bytes 17-18 fragment position within the frame, as byte18*2 + byte17>>7
    bytes 20+   the Annex-B payload fragment

Nothing marks the *last* fragment of a frame, so a frame is only known to be
complete when the next frame's first fragment arrives. That costs one frame of
latency, about 40 ms at 25 fps, and is inherent to the protocol.
"""

from __future__ import annotations

# HEVC NAL unit types we care about.
NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34
NAL_IDR_W_RADL = 19
NAL_IDR_N_LP = 20   # the Pocket's usual keyframe slice
NAL_CRA = 21
NAL_IDR = NAL_IDR_N_LP  # kept for callers that want "the" IDR type
NAL_AUD = 35         # access unit delimiter
NAL_DJI_MARKER = 63  # DJI's private per-frame marker

# No single HEVC access unit at this resolution is anywhere near this.
MAX_ACCESS_UNIT = 2_000_000

# The reframer's own ceiling, and deliberately far tighter than the whole-frame
# one above. It only ever holds a single access unit waiting for the delimiter
# that closes it, and units on this camera measure a couple of kilobytes, so
# this is a hundredfold headroom. Sizing it at MAX_ACCESS_UNIT instead would
# stop the memory growth but not the cost of finding it: every packet rescans
# the whole carry, so a loose bound leaves a quadratic scan that burns CPU for
# a stream already known to be malformed.
MAX_CARRY = 256_000

# Annex-B start code.
START_CODE = bytes([0, 0, 1])


def nal_type(first_byte: int) -> int:
    """HEVC NAL type from the first byte of the 2-byte NAL header."""
    return (first_byte >> 1) & 0x3F


def is_vcl(t: int) -> bool:
    """0..31 are coded slice NALs."""
    return t <= 31


def is_keyframe_nal(t: int) -> bool:
    """Parameter sets or a random-access slice."""
    return t in (NAL_VPS, NAL_SPS, NAL_PPS,
                 NAL_IDR_W_RADL, NAL_IDR_N_LP, NAL_CRA)


def is_random_access_slice(t: int) -> bool:
    """An actual decodable entry point, not merely a parameter set."""
    return t in (NAL_IDR_W_RADL, NAL_IDR_N_LP, NAL_CRA)


def strip_dji_marker(annex_b: bytes) -> bytes:
    """Drop DJI's `00 00 01 ff ...` marker so a standard decoder accepts the unit."""
    i = 0
    n = len(annex_b)
    while i + 4 <= n:
        if annex_b[i] == 0 and annex_b[i + 1] == 0 and annex_b[i + 2] == 1:
            if nal_type(annex_b[i + 3]) != NAL_DJI_MARKER:
                return annex_b[i:]
            i += 3
        else:
            i += 1
    return annex_b


def nal_units(annex_b: bytes) -> list[bytes]:
    """Split an Annex-B buffer into NAL units with start codes removed."""
    starts: list[int] = []
    i = 0
    n = len(annex_b)
    while i + 3 <= n:
        if annex_b[i] == 0 and annex_b[i + 1] == 0 and annex_b[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1

    out: list[bytes] = []
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        # Trim the trailing zero belonging to a 4-byte start code.
        while e > s and annex_b[e - 1] == 0:
            e -= 1
        if e > s:
            out.append(annex_b[s:e])
    return out


def strip_aud(annex_b: bytes) -> bytes:
    """Drop access unit delimiters.

    The camera's fragment counter advances one NAL late, so every unit we
    reassemble ends with the NEXT frame's AUD. An AUD announces the start of an
    access unit, so a trailing one tells the decoder more data is coming and it
    holds the frame back waiting for a continuation that arrives in the next
    packet instead. AUDs are optional, so removing them is simpler and safer
    than re-framing around them.
    """
    out = bytearray()
    for nal in nal_units(annex_b):
        if nal and nal_type(nal[0]) != NAL_AUD:
            out += START_CODE + nal
    return bytes(out) if out else annex_b


class AudReframer:
    """Re-cut the stream so each access unit begins at its delimiter.

    The camera's byte-16 fragment counter advances one NAL late, so every unit
    the depacketiser produces ends with the NEXT frame's access unit
    delimiter. Feeding those to a decoder gives it units that are each one NAL
    out of step: it decodes sporadically and stalls, which looks like an
    unstable link rather than a framing error.

    Carrying the trailing AUD forward puts every boundary back where the
    bitstream says it is.
    """

    def __init__(self) -> None:
        self.carry = b""
        self.emitted = 0
        self.discarded_head = 0
        self.overflows = 0

    def reset(self) -> None:
        self.carry = b""

    def feed(self, unit: bytes) -> list[bytes]:
        data = self.carry + unit
        starts = [i for i, nal_start in _aud_positions(data)]
        if len(starts) < 2:
            if not starts:
                self.discarded_head += 1   # nothing framed yet; drop the head
                self.carry = b""
            elif len(data) > MAX_CARRY:
                # One delimiter arrived and the one that would close the unit
                # never did. Carrying the bytes forward is right for a frame or
                # two and catastrophic beyond that: the buffer grows by every
                # packet, and _aud_positions rescans the whole of it each time,
                # so the memory cost is linear while the CPU cost is quadratic.
                # Left alone this exhausts memory -- observed as a MemoryError
                # in a panel that had been streaming for about ninety minutes,
                # alongside the decoder starving because nothing was ever
                # emitted. A real access unit here is a couple of kilobytes, so
                # anything past the whole-frame ceiling is malformed and the
                # only useful thing to do with it is let it go.
                self.overflows += 1
                self.carry = b""
            else:
                self.carry = data
            return []
        out = [data[starts[k]:starts[k + 1]] for k in range(len(starts) - 1)]
        self.carry = data[starts[-1]:]
        self.emitted += len(out)
        return out


def _aud_positions(data: bytes):
    """(offset of start code, offset of NAL header) for every AUD."""
    i, n = 0, len(data)
    while i + 4 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if nal_type(data[i + 3]) == NAL_AUD:
                yield (i, i + 3)
            i += 3
        else:
            i += 1


def has_keyframe(annex_b: bytes) -> bool:
    """True if the access unit contains a real random-access slice.

    Parameter sets alone are NOT enough. Starting on a unit that carries only
    VPS/SPS/PPS gives the decoder a configuration but no reference picture, so
    it accepts every subsequent packet and silently returns no frame -- no
    error, no warning, just a stream that decodes exactly nothing. Whether
    that happened depended on which unit we joined on, which made the failure
    look intermittent.
    """
    return any(is_random_access_slice(nal_type(nal[0]))
               for nal in nal_units(annex_b) if nal)


class HevcDepacketizer:
    """Feed every UDP payload; get back a complete access unit, or None.

    Loss-aware: fragment positions increment by exactly one, so a gap means a
    lost or reordered datagram. Such a frame is dropped rather than handed to
    the decoder, because a broken access unit stalls decoders far worse than a
    missing frame shows on screen.
    """

    def __init__(self) -> None:
        self.current_frame: int | None = None
        self.buffer = bytearray()
        self.last_position: int | None = None
        self.corrupt = False
        self.dropped_incomplete = 0
        self.frames_emitted = 0
        # Instrumentation: a depacketiser that silently stops emitting looks
        # identical to one that is never fed.
        self.fed = 0
        self.accepted = 0
        self.frame_changes = 0
        self.max_buffer = 0
        self.frame_ids = set()

    def feed(self, payload: bytes) -> bytes | None:
        self.fed += 1
        if len(payload) <= 20 or payload[6] != 0x02:
            return None
        self.accepted += 1

        frame_no = payload[16]
        position = payload[18] * 2 + (payload[17] >> 7)

        if len(self.frame_ids) < 64:
            self.frame_ids.add(frame_no)

        completed: bytes | None = None
        if self.current_frame is not None and self.current_frame != frame_no:
            self.frame_changes += 1
            if self.buffer:
                if self.corrupt:
                    self.dropped_incomplete += 1
                else:
                    completed = strip_dji_marker(bytes(self.buffer))
                    self.frames_emitted += 1
            self.buffer.clear()
            self.corrupt = False
            self.last_position = None

        self.current_frame = frame_no
        if self.last_position is not None and position != self.last_position + 1:
            self.corrupt = True
        self.last_position = position
        self.buffer += payload[20:]
        self.max_buffer = max(self.max_buffer, len(self.buffer))
        # A frame counter that never advances would grow this without bound.
        # Cap it rather than consume memory until the process dies.
        if len(self.buffer) > MAX_ACCESS_UNIT:
            self.buffer.clear()
            self.corrupt = True
            self.dropped_incomplete += 1
        return completed

    def reset(self) -> None:
        self.current_frame = None
        self.buffer.clear()
        self.last_position = None
        self.corrupt = False
        self.dropped_incomplete = 0
        self.frames_emitted = 0
        # Instrumentation: a depacketiser that silently stops emitting looks
        # identical to one that is never fed.
        self.fed = 0
        self.accepted = 0
        self.frame_changes = 0
        self.max_buffer = 0
        self.frame_ids = set()
