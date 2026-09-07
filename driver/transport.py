"""DUML-over-UDP datalink framing for the Osmo Pocket family.

Every datagram is `[8B transport header][payload]`. For command packets
(pktType 0x05) the payload is `[12B routing header][DUML frame]`.

Ported from OpenPocketCine `DumlTransport.swift`, itself asserted against
Osmosis captures.
"""

from dataclasses import dataclass

PKT_HANDSHAKE = 0x00  # session open
PKT_TELEMETRY = 0x01  # peer status push
PKT_VIDEO = 0x02  # HEVC live-view fragments
PKT_ACKED_DATA = 0x03
PKT_ACK = 0x04  # window acknowledgement
PKT_COMMAND = 0x05  # routing header + DUML frame

CAMERA_HOST = "192.168.2.1"
CAMERA_UDP_PORT = 9004
CAMERA_TCP_POKE_PORT = 7001


def transport_header(pkt_type: int, payload_len: int, session_id: int, seq: int) -> bytes:
    """8-byte header: [u16le 0x8000|total][u16le session][u16le seq][u8 pktType][u8 xor].

    `total` is 8 + payload_len; the trailing byte is the XOR of the other seven.
    """
    total = 8 + payload_len
    w0 = 0x8000 | (total & 0x3FFF)
    b = bytearray(
        [
            w0 & 0xFF,
            (w0 >> 8) & 0xFF,
            session_id & 0xFF,
            (session_id >> 8) & 0xFF,
            seq & 0xFF,
            (seq >> 8) & 0xFF,
            pkt_type & 0xFF,
        ]
    )
    x = 0
    for v in b:
        x ^= v
    b.append(x)
    return bytes(b)


def routing_header(seq: int, cmd_counter: int, drone: bool = False) -> bytes:
    """12-byte routing header for pktType 0x05.

    [u16le ack=seq-8][u16le seq][00 00 00 00][u8 counter][01][drone?60:00][00]

    Both seq fields live in our own command-seq space. Getting this wrong
    silently drops writes while reads keep flowing.
    """
    ack = (seq - 8) & 0xFFFF
    return bytes(
        [
            ack & 0xFF,
            (ack >> 8) & 0xFF,
            seq & 0xFF,
            (seq >> 8) & 0xFF,
            0, 0, 0, 0,
            cmd_counter & 0xFF,
            0x01,
            0x60 if drone else 0x00,
            0x00,
        ]
    )


def handshake_payload(base_seq: int) -> bytes:
    """40-byte session-open payload (pktType 0x00).

    First two bytes are `base_seq` LE -- use a fresh random 8-aligned value per
    connect; a fixed base can wedge the peer. The rest is a fixed template
    (proposed window 100, MTU 1472).
    """
    p = bytearray(
        [
            0x00, 0x00, 0x64, 0x00, 0x64, 0x00, 0xC0, 0x05, 0x14, 0x00, 0x00, 0x64, 0x00, 0x00,
            0x01, 0x90,
            0x01, 0xC0, 0x05, 0x14, 0x00, 0x00, 0x64, 0x00, 0x14, 0x00, 0x64, 0x00, 0xC0, 0x05,
            0x14, 0x00,
            0x00, 0x64, 0x00, 0x01, 0x01, 0x04, 0x01, 0x02,
        ]
    )
    p[0] = base_seq & 0xFF
    p[1] = (base_seq >> 8) & 0xFF
    return bytes(p)


@dataclass
class AckWindows:
    """The three independent receive windows echoed in a pktType-0x04 ACK.

    The camera uses separate sequence spaces for video, reliable data, and its
    extra status window.  Collapsing them into one cursor lets one stream
    acknowledge another and eventually stalls the downlink.
    """

    video: int = 0
    acked_data: int = 0
    extra: int = 0

    @classmethod
    def for_handshake(cls, base_seq: int) -> "AckWindows":
        """Initial unknown state; ACK serialization supplies wire fallbacks."""
        del base_seq  # fallback is an ACK-wire concern, not an advertised window
        return cls()


def ack_payload(windows: AckWindows, fallback_seq: int = 0) -> bytes:
    """26-byte pktType-0x04 payload.

    grp(video) + grp(ackedData) + grp(extra) + [0, 0], where
    grp(v) = [lo, hi, lo, hi, 0, 0, 0, 0]. Each stream must be echoed in its
    own group; the peer holds its downlink until it sees its cursor returned.
    Before reliable-data and extra windows are advertised, zero means unknown
    and is serialized as `fallback_seq`, preserving the handshake wire shape.
    """

    def grp(v: int) -> bytes:
        lo, hi = v & 0xFF, (v >> 8) & 0xFF
        return bytes([lo, hi, lo, hi, 0, 0, 0, 0])

    acked_data = windows.acked_data or fallback_seq
    extra = windows.extra or fallback_seq
    return (grp(windows.video) + grp(acked_data) + grp(extra)
            + b"\x00\x00")


def transport_seq(datagram: bytes) -> int | None:
    """Transport sequence, bytes 4-5. Used as the ACK cursor."""
    if len(datagram) < 6:
        return None
    return datagram[4] | (datagram[5] << 8)


def is_handshake(datagram: bytes) -> bool:
    """Session-open reply. Byte 6 is pktType; session/seq may differ from ours."""
    return len(datagram) >= 8 and datagram[6] == PKT_HANDSHAKE
