"""Read the M5Stack Core2 IMU stream off USB serial.

Line protocol (see firmware/core2_imu):

    IMU <pitch> <roll> <yawRate> <engaged> <btnB> <btnC>

    pitch    degrees, +up, gravity-referenced (absolute, no drift)
    roll     degrees, +right, gravity-referenced
    yawRate  degrees/second from the gyro (no magnetometer -> rate only)
    engaged  1 while button A is held (dead-man switch)
    btnB     1 on a fresh button B press (recenter)
    btnC     1 on a fresh button C press (flip)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

BAUD = 115200


@dataclass
class ImuSample:
    pitch: float = 0.0
    roll: float = 0.0
    yaw_rate: float = 0.0
    engaged: bool = False
    btn_b: bool = False
    btn_c: bool = False


def find_core2_port() -> str | None:
    """Core2 exposes a CP210x/CH9102 USB bridge."""
    for p in list_ports.comports():
        blob = f"{p.description} {p.manufacturer or ''} {p.hwid}".lower()
        if any(k in blob for k in ("cp210", "silicon labs", "ch9102", "ch340", "m5stack")):
            return p.device
    return None


class ImuLink:
    """Background reader. `latest` is always the most recent sample."""

    def __init__(self, port: str | None = None, baud: int = BAUD):
        self.port = port or find_core2_port()
        if not self.port:
            raise RuntimeError(
                "no Core2 serial port found -- pass --imu-port COMx "
                "(check Device Manager for the CP210x/CH9102 bridge)"
            )
        self.baud = baud
        self.latest = ImuSample()
        self._serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Edge-triggered button events, consumed by the caller.
        self._pending_b = False
        self._pending_c = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self._serial = serial.Serial(self.port, self.baud, timeout=0.2)
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name="imu-link", daemon=True)
        self._thread.start()
        log.info("IMU link on %s @ %d", self.port, self.baud)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> ImuLink:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def take_button_b(self) -> bool:
        with self._lock:
            v, self._pending_b = self._pending_b, False
        return v

    def take_button_c(self) -> bool:
        with self._lock:
            v, self._pending_c = self._pending_c, False
        return v

    def _read_loop(self) -> None:
        assert self._serial is not None
        while not self._stop.is_set():
            try:
                raw = self._serial.readline()
            except (serial.SerialException, OSError) as exc:
                log.warning("serial read failed: %s", exc)
                return
            if not raw:
                continue
            line = raw.decode("ascii", errors="ignore").strip()
            if not line.startswith("IMU "):
                if line:
                    log.debug("core2: %s", line)
                continue
            parts = line.split()
            if len(parts) != 7:
                continue
            try:
                sample = ImuSample(
                    pitch=float(parts[1]),
                    roll=float(parts[2]),
                    yaw_rate=float(parts[3]),
                    engaged=parts[4] == "1",
                    btn_b=parts[5] == "1",
                    btn_c=parts[6] == "1",
                )
            except ValueError:
                continue
            self.latest = sample
            if sample.btn_b or sample.btn_c:
                with self._lock:
                    self._pending_b |= sample.btn_b
                    self._pending_c |= sample.btn_c
