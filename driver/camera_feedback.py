"""Fresh camera facts, never optimistic setter echoes.

Protocol layouts: OpenPocketCine (Apache-2.0), commit
9b30b93572797c94db5ad9236fb746410f8d761f, CameraStatus.swift and
CameraControl.swift in Sources/OpenPocketViewCore. Existing notices apply.
https://github.com/erik-sutton95/OpenPocketCine/tree/9b30b93572797c94db5ad9236fb746410f8d761f/Sources/OpenPocketViewCore
Native Claude Sonnet supplied the initial Python decoder draft; reviewed here.
"""
from __future__ import annotations

import math
import struct
import threading
import time

FIELDS = ('recording', 'record_seconds', 'focus_mode', 'focus_point', 'zoom', 'color')
COLORS = {0x3F: 'normal', 0x3C: 'hdr', 0x17: 'd-log', 0x41: 'd-log2'}


def parse_subscribe(payload: bytes) -> tuple[str, bytes] | None:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 24 or payload[:2] != b'\x02\x06':
        return None
    length = struct.unpack_from('<H', payload, 13)[0]
    end = 15 + length
    if not 1 <= length <= 79 or end + 8 > len(payload):
        return None
    try:
        name = payload[15:end].decode('utf-8')
    except UnicodeDecodeError:
        return None
    size = struct.unpack_from('<H', payload, end + 6)[0]
    if end + 8 + size > len(payload):
        return None
    return name, bytes(payload[end + 8:end + 8 + size])


class CameraFeedback:
    def __init__(self, clock=time.monotonic, stale_after: float = 3.0):
        if isinstance(stale_after, bool) or not isinstance(stale_after, (float, int)) or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError('stale_after must be finite and positive')
        self.clock, self.stale_after = clock, float(stale_after)
        self._state: dict = {}
        self._lock = threading.RLock()

    def note(self, cmd_set: int, cmd_id: int, payload: bytes, now=None) -> bool:
        now = self.clock() if now is None else now
        if not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            return False
        if not isinstance(payload, (bytes, bytearray)):
            return False
        values = {}
        if (cmd_set, cmd_id) == (2, 0x80) and len(payload) >= 13:
            values['recording'] = bool(payload[0] & 0x80)
            if len(payload) >= 31:
                values['record_seconds'] = struct.unpack_from('<H', payload, 29)[0]
        elif (cmd_set, cmd_id) == (0, 0x99):
            parsed = parse_subscribe(payload)
            if parsed is None:
                return False
            name, v = parsed
            if name == 'cam_lens_state':
                if v and v[0] in (1, 2, 0xB1, 0xB2):
                    values['focus_mode'] = 'single' if v[0] in (1, 0xB1) else 'continuous'
                if len(v) >= 9:
                    x, y = struct.unpack_from('<ff', v, 1)
                    if all(math.isfinite(n) and 0 <= n <= 1 for n in (x, y)):
                        values['focus_point'] = {'x': x, 'y': y}
                if len(v) >= 16:
                    raw = struct.unpack_from('<H', v, 14)[0]
                    if 217 <= raw <= 2604:
                        values['zoom'] = raw / 217
            elif name == 'cam_image_effect' and len(v) >= 3 and v[2] in COLORS:
                values['color'] = COLORS[v[2]]
            elif name == 'cam_record_time' and len(v) >= 4:
                seconds = struct.unpack_from('<I', v)[0]
                if seconds <= 86400:
                    values['record_seconds'] = seconds
        accepted = False
        with self._lock:
            for key, value in values.items():
                if key not in self._state or now >= self._state[key][1]:
                    self._state[key] = (value, now)
                    accepted = True
        return accepted

    def snapshot(self, now=None) -> dict:
        now = self.clock() if now is None else now
        valid_time = isinstance(now, (float, int)) and math.isfinite(now) and now >= 0
        result = {}
        with self._lock:
            for key in FIELDS:
                value, stamp = self._state.get(key, (None, None))
                age = max(0.0, now - stamp) if valid_time and stamp is not None else None
                fresh = age is not None and now >= stamp and age < self.stale_after
                result[key] = {'value': (dict(value) if isinstance(value, dict) else value) if fresh else None,
                               'reported': fresh, 'age_s': round(age, 3) if age is not None else None}
        return result
