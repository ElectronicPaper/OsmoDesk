"""DUML frame builders for the connection spine, registration and the gimbal.

Opcodes are the ones OpenPocketCine confirmed on Osmo Pocket 3 / 4 / 4 Pro.
Anything not listed here is not verified -- do not guess opcodes, capture them.
"""

import math
import struct
from dataclasses import dataclass

from . import duml
from .duml import Frame

# The app identity the camera keys its remembered pairing approval on.
DEFAULT_IDENTIFIER = "284ae5b8d76b3375a04a6417ad71bea3"
DEFAULT_PIN = "osmo"

# Status keys pushed after 0x00/0x99 subscription, starting at this sub id.
SUBSCRIPTION_KEYS = [
    "cam_status",
    "cam_storage",
    "cam_expo_param",
    "cam_video_param_v2",
    "cam_record_time",
]
FIRST_SUB_ID = 0x69DF


def _le16(v: int) -> bytes:
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


def _le32(v: int) -> bytes:
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])


# --- BLE session spine ------------------------------------------------------


def session_wake(msg_id: int = 0x802B) -> Frame:
    """0x00/0x2B [04 00] -- wake the session before pairing."""
    return Frame(duml.SENDER_APP, duml.RX_SESSION, msg_id, duml.FLAG_REQUEST, 0x00, 0x2B, b"\x04\x00")


def session_keepalive(msg_id: int = 0x802B) -> Frame:
    """0x00/0x2B [01 01] -- repeat to hold the BLE session."""
    return Frame(duml.SENDER_APP, duml.RX_SESSION, msg_id, duml.FLAG_REQUEST, 0x00, 0x2B, b"\x01\x01")


def set_pairing_pin(pin: str = DEFAULT_PIN, identifier: str = DEFAULT_IDENTIFIER,
                    msg_id: int = 0x8092) -> Frame:
    """0x07/0x45. Reply [00 01] already paired, [00 02] approve on the camera."""
    return Frame(
        duml.SENDER_APP, duml.RX_WIFI, msg_id, duml.FLAG_REQUEST, 0x07, 0x45,
        duml.pack_string(identifier) + duml.pack_string(pin),
    )


def pair_approval_ack(seq: int) -> Frame:
    """The camera's first-time approval arrives as a 0x07/0x46 *request*; answer it."""
    return Frame(duml.SENDER_APP, duml.RX_WIFI, seq, duml.FLAG_RESPONSE, 0x07, 0x46, b"\x00")


def wake_ap(msg_id: int = 0x8053) -> Frame:
    """0x53/0x10 -> type 0x1C. Camera answers 01 00 00 00 and brings its AP up."""
    return Frame(duml.SENDER_APP, duml.RX_WAKE, msg_id, duml.FLAG_REQUEST, 0x53, 0x10, b"\x00\x00\x00\x00")


def get_wifi_ssid(msg_id: int = 0x8007) -> Frame:
    return Frame(duml.SENDER_APP, duml.RX_WIFI, msg_id, duml.FLAG_REQUEST, 0x07, 0x07, b"")


def get_wifi_password(msg_id: int = 0x800E) -> Frame:
    return Frame(duml.SENDER_APP, duml.RX_WIFI, msg_id, duml.FLAG_REQUEST, 0x07, 0x0E, b"")


# --- datalink registration --------------------------------------------------


def app_device_info(seq: int) -> Frame:
    """0x00/0x81 device-info, cmdType 4 (flags 0x80) -> DM368 (type 0x08, id 2)."""
    b = bytearray(62)  # "\0APP" + 37*00 + 02 + 8*00 + 02 08 + 10*00
    b[1], b[2], b[3] = 0x41, 0x50, 0x50
    b[41] = 0x02
    b[50], b[51] = 0x02, 0x08
    return Frame(duml.SENDER_APP, duml.RX_DM368_2, seq, duml.FLAG_ACK80, 0x00, 0x81, bytes(b))


_APP_PRESENCE = bytes(
    [0x17, 0x00, 0x46, 0x23, 0x7C, 0x41, 0x50, 0x50, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02]
)


def app_presence(seq: int) -> Frame:
    """0x00/0x88 -- re-send ~1 Hz to hold the session alive."""
    return Frame(duml.SENDER_APP, duml.RX_DM368_1, seq, duml.FLAG_REQUEST, 0x00, 0x88, _APP_PRESENCE)


def gimbal_init(seq: int) -> Frame:
    """0x03/0xDA -- gimbal register. Not a recenter."""
    return Frame(
        duml.SENDER_APP, duml.RX_GIMBAL_INIT, seq, duml.FLAG_REQUEST, 0x03, 0xDA,
        b"\x05\xff\xff\xff\xff",
    )


def subscribe(key: str, sub_id: int, seq: int) -> Frame:
    """0x00/0x99 status subscription.

    [02 02 00 00][subId u32-LE][00 00 00][innerLen u16-LE][nameLen u16-LE][name][00 00 00 00]
    innerLen = nameLen + 6, name unpadded.
    """
    nb = key.encode("utf-8")
    p = bytearray(b"\x02\x02\x00\x00")
    p += _le32(sub_id)
    p += b"\x00\x00\x00"
    p += _le16(len(nb) + 6)
    p += _le16(len(nb))
    p += nb
    p += b"\x00\x00\x00\x00"
    return Frame(duml.SENDER_APP, duml.RX_DM368_1, seq, duml.FLAG_REQUEST, 0x00, 0x99, bytes(p))


# --- gimbal -----------------------------------------------------------------
# Receiver 0x04. Mode/param writes ACK with flags 0x80; the stick is flags 0x00
# and is never acknowledged.


def _gimbal(cmd: int, payload: bytes, seq: int, flags: int = duml.FLAG_REQUEST) -> Frame:
    return Frame(duml.SENDER_APP, duml.RX_GIMBAL, seq, flags, 0x04, cmd, payload)


def gimbal_stick(axis0: int, axis1: int, seq: int = 0) -> Frame:
    """0x04/0x01, 10 bytes, no ACK.

    axis0 = tilt (up = max, down = min), axis1 = pan (left = min, right = max).
    Both u16-LE, centre 1024, travel +/-550 -> 474..1574.
    """
    p = _le16(axis0) + b"\x00\x00" + _le16(axis1) + b"\x00\x80\x22\x00"
    return _gimbal(0x01, p, seq, flags=duml.FLAG_NOTIFY)


def gimbal_recenter(seq: int = 0) -> Frame:
    """0x04/0x4C [FE 08] -- the Mimo recenter button."""
    return _gimbal(0x4C, b"\xfe\x08", seq)


def gimbal_flip(seq: int = 0) -> Frame:
    """0x04/0x4C [FE 09] -- front <-> selfie toggle."""
    return _gimbal(0x4C, b"\xfe\x09", seq)


def gimbal_follow(seq: int = 0) -> Frame:
    """0x04/0x4C [02 08] -- Follow / Tilt Locked family."""
    return _gimbal(0x4C, b"\x02\x08", seq)


def gimbal_fpv(seq: int = 0) -> Frame:
    """0x04/0x4C [01 08] -- FPV mode."""
    return _gimbal(0x4C, b"\x01\x08", seq)


def gimbal_params_get(seq: int = 0) -> Frame:
    """0x04/0x50 GET params 04 (tilt lock) and 05 (speed)."""
    return _gimbal(0x50, b"\x01\x04\x05", seq)


def set_gimbal_speed(speed: int, seq: int = 0) -> Frame:
    """0x04/0x50 SET param 05. 0 = Fast, 1 = Default, 2 = Slow."""
    return _gimbal(0x50, bytes([0x00, 0x05, 0x01, speed & 0xFF]), seq)


def set_gimbal_tilt_lock(lock: int, seq: int = 0) -> Frame:
    """0x04/0x50 SET param 04. 0 = Follow, 1 = Tilt Locked."""
    return _gimbal(0x50, bytes([0x00, 0x04, 0x01, lock & 0xFF]), seq)


# --- camera -----------------------------------------------------------------
# Receiver 0x01 on the Pocket family.


def record(start: bool, seq: int = 0) -> Frame:
    """0x02/0x02 -- [01] start, [00] stop. Verify against the camera body."""
    return Frame(duml.SENDER_APP, duml.RX_CAMERA, seq, duml.FLAG_REQUEST,
                 0x02, 0x02, bytes([1 if start else 0]))


def photo(seq: int = 0) -> Frame:
    """0x02/0x01 -- shutter. Reported as 0xD9 while in video mode."""
    return Frame(duml.SENDER_APP, duml.RX_CAMERA, seq, duml.FLAG_REQUEST,
                 0x02, 0x01, bytes([1]))


# --- live view --------------------------------------------------------------

LIVE_VIEW_RECEIVER_POCKET = 0x08
LIVE_VIEW_RECEIVER_NANO = 0x41


def live_view_enable(seq: int = 0, receiver: int = LIVE_VIEW_RECEIVER_POCKET) -> Frame:
    """0x09/0xA8 -- start the pktType-0x02 video stream on the same UDP socket.

    The payload is exact; a plausible-looking guess is silently accepted and
    does nothing. Receiver is 0x08 on the Pocket family, 0x41 on the Nano.
    """
    return Frame(
        duml.SENDER_APP, receiver, seq, duml.FLAG_REQUEST, 0x09, 0xA8,
        bytes([0x00, 0x04, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
    )


# --- gimbal telemetry -------------------------------------------------------


def parse_gimbal_position(payload: bytes) -> tuple[float, float, float] | None:
    """Deprecated shim. Use `parse_gimbal_attitude`.

    Kept so nothing silently reads the old, wrong offsets: returns the angle
    triple from the current parser, or None.
    """
    att = parse_gimbal_attitude(payload)
    return None if att is None else (att.pitch, att.yaw, att.yaw_alt)


@dataclass(frozen=True)
class GimbalAttitude:
    """One 0x04/0x05 heartbeat (~20 Hz), as measured on an Osmo Pocket 4 Pro.

    The Pocket 3 notes describe a 12-byte int16 pitch/roll/yaw payload. The
    Pocket 4 Pro sends 50 bytes with a different shape, derived here by moving
    the gimbal by hand and watching which offsets change:

        0   int16 LE   pitch, tenths of a degree, wraps at +/-180
        4   int16 LE   yaw, tenths of a degree
        8   int16 LE   yaw_alt, tenths of a degree
        12  uint32 LE  monotonic timestamp, ~100 ticks per frame
        24  float32 LE  \\
        28  float32 LE   |  unit quaternion, |q| == 1.0 to 5 decimal places
        32  float32 LE   |
        36  float32 LE  /

    Field identity was established by driving one stick axis at a time and
    watching which fields responded (`run.py --map-axes`):

        tilt +/-  ->  pitch moves ~17 deg, yaw and yaw_alt stay within 0.2
        pan  +/-  ->  yaw and yaw_alt move ~17 deg with OPPOSITE signs,
                      pitch stays at 0.0

    So `yaw` and `yaw_alt` are two representations of the same rotation (body
    frame versus motor joint), and nothing in this frame reports roll.

    A stick tilt-up command DECREASES pitch; see `gimbal.TILT_SIGN`.

    The quaternion component order (w-first vs w-last) is NOT established, so
    no Euler conversion is offered. `rotation_delta` compares two attitudes
    without needing to know the order.
    """

    pitch: float
    yaw: float
    yaw_alt: float
    timestamp: int
    quaternion: tuple[float, float, float, float]

    @property
    def quaternion_norm(self) -> float:
        return math.sqrt(sum(c * c for c in self.quaternion))


def parse_gimbal_attitude(payload: bytes) -> GimbalAttitude | None:
    if len(payload) < 40:
        return None

    def i16(o: int) -> int:
        v = payload[o] | (payload[o + 1] << 8)
        return v - 0x10000 if v & 0x8000 else v

    quat = struct.unpack_from("<4f", payload, 24)
    return GimbalAttitude(
        pitch=i16(0) / 10.0,
        yaw=i16(4) / 10.0,
        yaw_alt=i16(8) / 10.0,
        timestamp=struct.unpack_from("<I", payload, 12)[0],
        quaternion=quat,
    )


def rotation_delta(a: GimbalAttitude, b: GimbalAttitude) -> float:
    """Angle in degrees between two attitudes.

    Uses |dot(q1, q2)| so it is independent of both component order and the
    quaternion double-cover, which makes it a reliable "did it move at all"
    test without having pinned down the field order.
    """
    dot = abs(sum(x * y for x, y in zip(a.quaternion, b.quaternion)))
    return math.degrees(2.0 * math.acos(min(1.0, dot)))
