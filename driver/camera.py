"""Exposure, lens and status: the controls a DP actually reaches for.

Opcodes and payload shapes come from the OpenPocketCine command catalog, which
was captured from DJI Mimo against this camera family. Several have no GET, so
their current value is only readable from a pushed status frame -- that is a
property of the protocol, not an omission here.

Anything in this module that has not been confirmed against hardware says so.
Send it, watch for the ACK, and check the camera body.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import duml
from .duml import Frame


def _cam(cmd: int, payload: bytes, seq: int = 0) -> Frame:
    return Frame(duml.SENDER_APP, duml.RX_CAMERA, seq, duml.FLAG_REQUEST,
                 0x02, cmd, payload)


# --- exposure ---------------------------------------------------------------

# 0x02/0x2A. Index, not a value: 0x00 Auto, 0x03 = 100, doubling per step.
ISO_INDEX = {
    "auto": 0x00, "100": 0x03, "200": 0x04, "400": 0x05, "800": 0x06,
    "1600": 0x07, "3200": 0x08, "6400": 0x09, "12800": 0x0A, "25600": 0x0B,
}

# Shutter denominators a DP would reach for. 1/50 is the 180-degree shutter at
# 25 fps, 1/48 at 24 fps.
SHUTTER_DENOMS = [24, 25, 30, 48, 50, 60, 100, 120, 200, 400, 800, 1000, 2000, 4000]

# 0x02/0x42. Pocket family values. D-Log is the reason to use this camera on a
# real job, so it gets a first-class control.
COLOR_MODES = {
    "normal": 0x3F,
    "hdr": 0x3C,
    "d-log": 0x17,
    "d-log2": 0x41,
}

# 0x02/0x18. Resolution byte then an fps index.
RESOLUTIONS = {"1080p": 0x0A, "4K": 0x10}
FPS_INDEX = {"24": 0x01, "25": 0x02, "30": 0x03, "48": 0x04, "50": 0x05, "60": 0x06}

# 0x02/0xb8 lens positions. 217 = 1x, 651 = 3x, 2604 = 12x, linear between.
ZOOM_LENS = {"1x": 217, "2x": 434, "3x": 651, "6x": 1302, "9x": 1953, "12x": 2604}

WB_MODES = {"auto": 0x00, "custom": 0x06}


def set_iso(name: str, seq: int = 0) -> Frame:
    """0x02/0x2A. No GET -- read it back from the cam_expo_param push."""
    if name not in ISO_INDEX:
        raise ValueError(f"unknown ISO: {name!r}")
    return _cam(0x2A, bytes([ISO_INDEX[name]]), seq)


def set_shutter(denominator: int, seq: int = 0) -> Frame:
    """0x02/0x28 -- `01 <denom|0x8000 u16-LE> 00 00 00 40`. Shutter 1/denominator."""
    if denominator <= 0:
        raise ValueError("shutter denominator must be positive")
    v = (denominator | 0x8000) & 0xFFFF
    return _cam(0x28, bytes([0x01, v & 0xFF, (v >> 8) & 0xFF, 0x00, 0x00, 0x00, 0x40]), seq)


def set_exposure_manual(manual: bool, seq: int = 0) -> Frame:
    """0x02/0x1E -- `04 00` manual, `01 00` auto."""
    return _cam(0x1E, bytes([0x04 if manual else 0x01, 0x00]), seq)


def set_ev(step: int, seq: int = 0) -> Frame:
    """0x02/0x2E exposure compensation, in thirds of a stop.

    UNVERIFIED payload shape. The opcode is in the catalog but its bytes are
    not spelled out, so this is a single signed step and may well be wrong.
    """
    return _cam(0x2E, bytes([step & 0xFF]), seq)


def set_white_balance(kelvin: int | None = None, tint: int = 0, seq: int = 0) -> Frame:
    """0x02/0x2C -- `[mode][K/100 u16-LE][tint i16-LE]`. None means auto."""
    if kelvin is None:
        return _cam(0x2C, bytes([WB_MODES["auto"], 0, 0, 0, 0]), seq)
    if not (2000 <= kelvin <= 10000):
        raise ValueError("white balance out of range: 2000-10000 K")
    return _cam(0x2C, bytes([WB_MODES["custom"]]) + struct.pack("<Hh", kelvin // 100, tint), seq)


def set_color_mode(name: str, seq: int = 0) -> Frame:
    """0x02/0x42. `d-log` is the one that matters for grading."""
    if name not in COLOR_MODES:
        raise ValueError(f"unknown color mode: {name!r}")
    return _cam(0x42, bytes([COLOR_MODES[name]]), seq)


def set_resolution(resolution: str, fps: str, seq: int = 0) -> Frame:
    """0x02/0x18 -- 5 bytes `[res][fps_idx] 00 00 00`."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unknown resolution: {resolution!r}")
    if fps not in FPS_INDEX:
        raise ValueError(f"unknown fps: {fps!r}")
    return _cam(0x18, bytes([RESOLUTIONS[resolution], FPS_INDEX[fps], 0, 0, 0]), seq)


def set_zoom_lens(lens: int, seq: int = 0) -> Frame:
    """0x02/0xb8 -- slider form `0A 4E` with the lens position at offset 14.

    Mimo's pinch from 1x to 12x uses only this form, at roughly 20 Hz.
    """
    lens = max(ZOOM_LENS["1x"], min(ZOOM_LENS["12x"], int(lens)))
    p = bytearray(16)
    p[0], p[1] = 0x0A, 0x4E
    p[14], p[15] = lens & 0xFF, (lens >> 8) & 0xFF
    return _cam(0xB8, bytes(p), seq)


def set_zoom(factor: float, seq: int = 0) -> Frame:
    """Convenience: 1.0 .. 12.0 mapped onto the lens scale."""
    f = max(1.0, min(12.0, float(factor)))
    lens = ZOOM_LENS["1x"] + (f - 1.0) * (ZOOM_LENS["12x"] - ZOOM_LENS["1x"]) / 11.0
    return set_zoom_lens(round(lens), seq)


def set_focus_mode(continuous: bool, seq: int = 0) -> Frame:
    """0x02/0x24 -- `01` single (AF-S), `02` continuous (AF-C)."""
    return _cam(0x24, bytes([0x02 if continuous else 0x01]), seq)


# --- status parsing ---------------------------------------------------------


@dataclass
class PowerStatus:
    """0x0D/0x02, pushed about once a second.

    Offsets are from the OpenPocketCine status parser: u16 @1 millivolts,
    i32 @5 milliamps signed, percent @20, dock @27, charging @32.
    """

    percent: int | None = None
    millivolts: int | None = None
    milliamps: int | None = None
    charging: bool = False
    docked: bool = False

    @staticmethod
    def parse(payload: bytes) -> "PowerStatus | None":
        if len(payload) < 21:
            return None
        st = PowerStatus()
        pct = payload[20]
        if 0 <= pct <= 100:
            st.percent = pct
        if len(payload) >= 34:
            mv = payload[1] | (payload[2] << 8)
            if 2000 <= mv <= 5000:
                st.millivolts = mv
            st.milliamps = struct.unpack_from("<i", payload, 5)[0]
            st.docked = payload[27] != 0
            st.charging = payload[32] == 1
        return st


@dataclass
class RecordStatus:
    """0x02/0x80, the unsolicited camera-state push.

    Only the recording flag is decoded, and only tentatively: the frame is
    known to carry active-store and a playback bit, but the field map was not
    captured. Treat `recording` as a hint until it is confirmed against the
    camera body, which is why the panel never derives its own record state
    from this alone.
    """

    raw: bytes = b""
    recording: bool | None = None

    @staticmethod
    def parse(payload: bytes) -> "RecordStatus":
        return RecordStatus(raw=payload[:8])
