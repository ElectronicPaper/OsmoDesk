"""DUML wire framing for DJI Osmo cameras.

Frame layout (total = 13 + len(payload)):
    55 | len_lo | (ver<<2)|len_hi | crc8(bytes[0:3]) |
    sender | receiver | seq:u16le | flags | cmdSet | cmdId | payload | crc16:u16le

CRC parameters are the reflected-domain forms:
    CRC8  init 0x77, poly 0x8C  (normal form: init 0xEE, poly 0x31)
    CRC16 init 0x3692, poly 0x8408 (normal form: init 0x496C, poly 0x1021)

Note on `seq`: OpenPocketCine encodes it little-endian and is verified on
Pocket 4 / 4 Pro hardware; lib-osmo-ble documents big-endian from Pocket 3 BLE
captures. We follow the Pocket 4 implementation. The field is a counter the
camera echoes back, so a wrong endianness shows up as mismatched replies
rather than rejected frames.
"""

from dataclasses import dataclass, field

# --- addressing -------------------------------------------------------------
# Receivers are packed as (id << 5) | type.

def rx(type_: int, id_: int = 0) -> int:
    return ((id_ << 5) | type_) & 0xFF


SENDER_APP = 0x02  # id 0, type 2

RX_CAMERA = rx(0x01)  # 0x01 -- camera
RX_GIMBAL = rx(0x04)  # 0x04 -- gimbal
RX_WIFI = rx(0x07)  # 0x07 -- Wi-Fi subsystem
RX_SESSION = rx(0x10, 7)  # 0xF0 -- session / heartbeat
RX_WAKE = rx(0x1C)  # 0x1C -- 0x53/0x10 AP wake
RX_DM368_1 = rx(0x08, 1)  # 0x28 -- presence, subscriptions
RX_DM368_2 = rx(0x08, 2)  # 0x48 -- device info
RX_GIMBAL_INIT = rx(0x03)  # 0x03 -- 0x03/0xDA


# --- flags ------------------------------------------------------------------

FLAG_REQUEST = 0x40
FLAG_RESPONSE = 0xC0
FLAG_NOTIFY = 0x00
FLAG_ACK80 = 0x80  # gimbal / cmdType-4 acknowledgement


# --- checksums --------------------------------------------------------------


def crc8(data: bytes) -> int:
    c = 0x77
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8C) if (c & 1) else (c >> 1)
    return c & 0xFF


def crc16(data: bytes) -> int:
    c = 0x3692
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8408) if (c & 1) else (c >> 1)
    return c & 0xFFFF


# --- frame ------------------------------------------------------------------


@dataclass
class Frame:
    sender: int
    receiver: int
    seq: int
    flags: int
    cmd_set: int
    cmd_id: int
    payload: bytes = field(default=b"")

    @property
    def opcode(self) -> tuple[int, int]:
        return (self.cmd_set, self.cmd_id)

    def __repr__(self) -> str:
        return (
            f"Frame(0x{self.cmd_set:02X}/0x{self.cmd_id:02X} "
            f"snd=0x{self.sender:02X} rcv=0x{self.receiver:02X} "
            f"flags=0x{self.flags:02X} seq={self.seq} "
            f"payload={self.payload.hex(' ') if self.payload else '-'})"
        )


def encode(f: Frame) -> bytes:
    total = 13 + len(f.payload)
    if total > 0x3FF:
        raise ValueError(f"DUML frame too long: {total}")
    b = bytearray(
        [
            0x55,
            total & 0xFF,
            (1 << 2) | ((total >> 8) & 0x03),
        ]
    )
    b.append(crc8(bytes(b)))
    b += bytes(
        [
            f.sender & 0xFF,
            f.receiver & 0xFF,
            f.seq & 0xFF,
            (f.seq >> 8) & 0xFF,
            f.flags & 0xFF,
            f.cmd_set & 0xFF,
            f.cmd_id & 0xFF,
        ]
    )
    b += f.payload
    c = crc16(bytes(b))
    b += bytes([c & 0xFF, (c >> 8) & 0xFF])
    return bytes(b)


def decode(data: bytes) -> tuple[Frame, int] | None:
    """Decode one frame at the head of `data`. Returns (frame, bytes_consumed)."""
    if len(data) < 13 or data[0] != 0x55:
        return None
    total = data[1] | ((data[2] & 0x03) << 8)
    if (data[2] >> 2) != 1 or total < 13 or len(data) < total:
        return None
    f = data[:total]
    if crc8(f[:3]) != f[3]:
        return None
    got = f[total - 2] | (f[total - 1] << 8)
    if crc16(f[: total - 2]) != got:
        return None
    return (
        Frame(
            sender=f[4],
            receiver=f[5],
            seq=f[6] | (f[7] << 8),
            flags=f[8],
            cmd_set=f[9],
            cmd_id=f[10],
            payload=bytes(f[11 : total - 2]),
        ),
        total,
    )


def scan_frames(raw: bytes) -> list[Frame]:
    """Every CRC-valid DUML frame in `raw`.

    Scans byte-at-a-time so it finds frames inside the UDP transport/routing
    wrapper without modelling that wrapper. Validating both CRCs is what makes
    byte-at-a-time safe.
    """
    out: list[Frame] = []
    i, n = 0, len(raw)
    while i + 13 <= n:
        if raw[i] != 0x55:
            i += 1
            continue
        got = decode(raw[i:])
        if got is None:
            i += 1
            continue
        out.append(got[0])
        i += 1
    return out


# --- string packing ---------------------------------------------------------


def pack_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return bytes([len(b)]) + b


def unpack_status_string(p: bytes) -> str:
    """A Wi-Fi-subsystem reply is [status:1][len:1][utf8...]."""
    if len(p) < 2:
        return ""
    end = min(2 + p[1], len(p))
    return p[2:end].decode("utf-8", errors="replace")
