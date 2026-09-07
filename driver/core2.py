"""Bidirectional link to an M5Stack Core2 control surface over USB serial.

The Core2 is not just an IMU any more: it has a touchscreen, three buttons, a
speaker, a vibration motor and two bars of RGB LEDs. Those outputs are only
worth having if they show the *rig's* state, so the link runs both ways --
motion and intent up, state down.

Line-based ASCII on purpose. It is trivially debuggable with a serial monitor,
survives a partial line after a reset, and at these rates the framing overhead
is irrelevant next to being able to see what the box is saying.

    Core2 -> PC
      H <fw> <features>            hello, sent on boot and on request
      I <pitch> <roll> <yawrate> [<pitchrate>]   attitude, ~100 Hz
      E <0|1> [k=v ...]            clutch released / grabbed (+ diagnostics)
      J <tilt> <pan>               jog pad, unit floats
      B <name>                     button or touch action
      P <ok>                       pong

    PC -> Core2
      S <k>=<v> ...                rig state for the LEDs, screen and haptics
      A <pitch> <yaw>              live gimbal attitude
      Z <ms>                       vibrate
      ?                            request a hello
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

BAUD = 115200

# How long to wait before trying the port again after it goes away. Short
# enough that a replug feels instant, long enough that a permanently absent
# box does not spin the CPU or fill the log.
RECONNECT_DELAY_S = 1.0

# The box streams attitude at about 100 Hz whenever it is alive, so silence is
# not a quiet moment -- it means the far end is gone. On Windows an unplugged
# USB serial adapter does not always raise: the handle survives and reads just
# return nothing forever, which looks exactly like a working link with a bored
# operator. Timing it out is the only reliable way to notice.
SILENCE_TIMEOUT_S = 3.0

# Actions the Core2 can send. Anything else is logged and ignored, so old
# firmware talking to a new driver degrades instead of crashing.
ACTIONS = {
    "grab", "release", "arm", "disarm", "go", "rec", "photo",
    "recenter", "flip", "wider", "tighter", "slower", "faster", "abort",
    "tilt_lock", "pan_lock", "select", "gain_next",
    "profile_FLAT", "profile_UPRIGHT", "profile_GRIP", "profile_LEFT",
    # Settings sub-pages on the box. The light toggles are Core2-local and are
    # accepted here only so they are not logged as unknown every press.
    "reconnect", "limitleds_0", "limitleds_1", "beacon_0", "beacon_1",
    # These are emitted by the Core2 settings panes.  Keep the wire
    # vocabulary explicit here: _handle is the trust boundary between a USB
    # device and CameraSession, so a handler below is useless if this filter
    # drops the action first.
    "speed_SLOW", "speed_NORMAL", "speed_FAST",
    "invert_tilt_0", "invert_tilt_1", "invert_pan_0", "invert_pan_1",
    # The header record key. The firmware has always sent these two; only
    # "rec" was accepted here, and "rec" had no stop, so hold-to-record over
    # USB never did anything. tests/test_firmware_source.py now derives the
    # emitted vocabulary from main.cpp and checks it against this set.
    "record_start", "record_stop",
    # Box-local, accepted so they are not logged as unknown every press.
    "feel_defaults",
    # From the MOVES run key while a host timelapse is running: stop between
    # frames, which leaves a resumable progress file, unlike abort.
    "tl_stop",
    # Feel keys. The host has one stick, so jog and hand feel land on the
    # same ramp/speed there; the template has no host counterpart yet.
    "smooth_CRISP", "smooth_FLUID", "smooth_GLIDE",
    "tilt_response_FINE", "tilt_response_BALANCED", "tilt_response_DIRECT",
    "pan_response_FINE", "pan_response_BALANCED", "pan_response_DIRECT",
    "tilt_stability_QUIET", "tilt_stability_BALANCED",
    "tilt_stability_RESPONSIVE",
    "pan_stability_QUIET", "pan_stability_BALANCED",
    "pan_stability_RESPONSIVE",
    "jog_speed_SLOW", "jog_speed_NORMAL", "jog_speed_FAST",
    "jog_smooth_CRISP", "jog_smooth_FLUID", "jog_smooth_GLIDE",
    "template_follow", "template_rate",
}


@dataclass
class ImuSample:
    pitch: float = 0.0
    roll: float = 0.0
    yaw_rate: float = 0.0
    pitch_rate: float = 0.0
    at: float = 0.0


@dataclass
class Core2Status:
    connected: bool = False
    port: str | None = None
    firmware: str = ""
    features: set[str] = field(default_factory=set)
    samples: int = 0
    last_sample_at: float = 0.0
    engaged: bool = False

    @property
    def stale(self) -> bool:
        return not self.last_sample_at or (time.time() - self.last_sample_at) > 1.0

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "firmware": self.firmware,
            "features": sorted(self.features),
            "samples": self.samples,
            "engaged": self.engaged,
            "stale": self.stale,
        }


# The USB-serial bridges M5Stack ships on the Core2 and its siblings. Matched
# by VID:PID first because that is what the device IS; the description is what
# the operating system decided to call it, and on Linux that is frequently the
# literal string "n/a". A build that only matched descriptions found the board
# on Windows and not on the Pi.
#
#   10c4:ea60  Silicon Labs CP210x   -- the Core2's own bridge, measured
#   1a86:55d4  WCH CH9102F           -- later M5 boards
#   1a86:7523  WCH CH340             -- clones and older units
KNOWN_BRIDGES = {(0x10C4, 0xEA60), (0x1A86, 0x55D4), (0x1A86, 0x7523)}

# Kept as a fallback for a bridge not in the table above: a new revision should
# still be found, just less certainly.
BRIDGE_HINTS = ("cp210", "silicon labs", "ch9102", "ch340", "m5stack")


def find_port() -> str | None:
    """The Core2 shows up as a CP210x or CH9102 USB bridge."""
    ports = list(list_ports.comports())
    for p in ports:
        if (p.vid, p.pid) in KNOWN_BRIDGES:
            return p.device
    for p in ports:
        blob = f"{p.description} {p.manufacturer or ''} {p.hwid}".lower()
        if any(k in blob for k in BRIDGE_HINTS):
            return p.device
    return None


class Core2Link:
    """Reader thread plus a writer, with callbacks for actions.

    `on_action(name)` fires for a button or touch event. `on_engage(bool)`
    fires for the clutch. Both are called from the reader thread, so they must
    be quick and must not block on the serial port.
    """

    def __init__(self, port: str | None = None, baud: int = BAUD):
        # An explicitly requested port is honoured for the life of the link. A
        # discovered one is re-discovered on every reconnect, because Windows
        # is free to hand the same device a different COM number when it comes
        # back and the old name would then never open again.
        self._requested_port = port
        self.port = port or find_port()
        self.baud = baud
        self.latest = ImuSample()
        self.status = Core2Status(port=self.port)
        self.on_action = None
        self.on_engage = None
        self.on_jog = None

        self._serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._last_rx = 0.0
        self._last_state_sent = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start supervising the link. Does not require the box to be present.

        Opening the port used to happen here, and the reader thread simply
        returned the first time a read failed. Unplugging the cable therefore
        ended the link permanently: the thread was gone, nothing reopened the
        port, and replugging did nothing at all. The link is now supervised, so
        the cable can come and go as often as it likes -- and a box plugged in
        after the session started is picked up too, which the old code also
        could not do.
        """
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="core2",
                                        daemon=True)
        self._thread.start()

    def _supervise(self) -> None:
        announced_missing = False
        while not self._stop.is_set():
            port = self._requested_port or find_port()
            if not port:
                if not announced_missing:
                    log.info("no Core2 serial port yet -- will keep looking "
                             "(pass --imu-port COMx to pin one)")
                    announced_missing = True
                self._stop.wait(RECONNECT_DELAY_S)
                continue
            announced_missing = False
            try:
                ser = serial.Serial(port, self.baud, timeout=0.2)
            except (serial.SerialException, OSError) as exc:
                log.debug("core2 open %s failed: %s", port, exc)
                self._stop.wait(RECONNECT_DELAY_S)
                continue

            with self._write_lock:
                self._serial = ser
            self.port = port
            self.status.port = port
            self.status.connected = True
            self._last_rx = time.time()
            # The box only re-announces itself on its own boot. A replug that
            # did not reset it would otherwise leave us with no firmware
            # string, so ask.
            self._last_state_sent = ""      # force a full state resend
            self.send("?")
            log.info("Core2 link on %s", port)

            self._read_until_lost()
            self._drop(ser)
            if not self._stop.is_set():
                log.info("Core2 link lost -- reconnecting")
                self._stop.wait(RECONNECT_DELAY_S)

    def _drop(self, ser: "serial.Serial") -> None:
        """Close a dead handle and stop claiming anything about the box.

        The firmware string and feature set are cleared deliberately: the next
        thing plugged into that port may not be running what the last one was,
        and a stale version number is worse than none.
        """
        with self._write_lock:
            if self._serial is ser:
                self._serial = None
        try:
            ser.close()
        except Exception:
            pass
        self.status.connected = False
        self.status.firmware = ""
        self.status.features = set()
        self.status.engaged = False
        # A clutch held at the moment the cable went is not held any more.
        if self.on_engage:
            try:
                self.on_engage(False)
            except Exception:
                log.exception("on_engage raised during disconnect")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._write_lock:
            ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self.status.connected = False

    def __enter__(self) -> "Core2Link":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- downlink -----------------------------------------------------------

    def send(self, line: str) -> None:
        """Never let a dead cable take down the caller.

        The handle is read and used under the same lock the supervisor uses to
        swap it, so a reconnect happening mid-write cannot leave this holding a
        closed port.
        """
        payload = (line + "\n").encode("ascii", "ignore")
        try:
            with self._write_lock:
                if self._serial is None:
                    return
                self._serial.write(payload)
        except (serial.SerialException, OSError) as exc:
            log.debug("core2 write failed: %s", exc)

    def send_state(self, **kv) -> None:
        """Push rig state. Identical states are not re-sent.

        The Core2 redraws and re-lights on every state line, so spamming an
        unchanged state at 20 Hz would make the screen flicker and waste the
        link. Attitude has its own message because it genuinely does change
        every frame.
        """
        line = "S " + " ".join(f"{k}={_fmt(v)}" for k, v in sorted(kv.items()))
        if line != self._last_state_sent:
            self._last_state_sent = line
            self.send(line)

    def send_attitude(self, pitch: float | None, yaw: float | None) -> None:
        if pitch is None or yaw is None:
            return
        self.send(f"A {pitch:.1f} {yaw:.1f}")

    def vibrate(self, ms: int = 40) -> None:
        self.send(f"Z {int(max(0, min(2000, ms)))}")

    # -- uplink -------------------------------------------------------------

    def _read_until_lost(self) -> None:
        """Read until the far end goes away. Returns so the supervisor can
        reopen; it must never end the thread."""
        ser = self._serial
        if ser is None:
            return
        while not self._stop.is_set():
            try:
                raw = ser.readline()
            except (serial.SerialException, OSError) as exc:
                log.info("core2 read failed: %s", exc)
                return
            if not raw:
                # Not necessarily idle -- see SILENCE_TIMEOUT_S. A box that is
                # alive is never quiet for this long.
                if time.time() - self._last_rx > SILENCE_TIMEOUT_S:
                    log.info("core2 silent for %.0fs -- assuming the cable is out",
                             SILENCE_TIMEOUT_S)
                    return
                continue
            self._last_rx = time.time()
            try:
                line = raw.decode("ascii", errors="ignore").strip()
            except Exception:
                continue
            if line:
                self._handle(line)

    def _handle(self, line: str) -> None:
        kind, _, rest = line.partition(" ")
        if kind == "I":
            parts = rest.split()
            if len(parts) < 3:
                return
            try:
                # Field 4 (pitch rate) is optional: firmware before 1.4 sends
                # three, and feedforward simply reads zero there.
                self.latest = ImuSample(
                    pitch=float(parts[0]), roll=float(parts[1]),
                    yaw_rate=float(parts[2]),
                    pitch_rate=float(parts[3]) if len(parts) > 3 else 0.0,
                    at=time.time())
            except ValueError:
                return
            self.status.samples += 1
            self.status.last_sample_at = self.latest.at
        elif kind == "E":
            # First token only. The firmware appends diagnostic fields
            # (`E 1 btnB=0 scr=1 since=0`, `E 0 reason=linklost`), and matching
            # the whole remainder made every grab parse as a release.
            engaged = rest.split()[0] == "1" if rest.split() else False
            self.status.engaged = engaged
            if self.on_engage:
                try:
                    self.on_engage(engaged)
                except Exception:
                    log.exception("on_engage raised")
        elif kind == "J":
            # Jog pad on the box: tilt and pan as unit floats. This was being
            # sent by the firmware and silently dropped here, which is why the
            # Core2 jog page did nothing at all.
            parts = rest.split()
            if len(parts) < 2 or self.on_jog is None:
                return
            try:
                tilt, pan = float(parts[0]), float(parts[1])
            except ValueError:
                return
            try:
                self.on_jog(tilt, pan)
            except Exception:
                log.exception("on_jog raised")
        elif kind == "B":
            name = rest.strip()
            if name not in ACTIONS:
                log.debug("core2: unknown action %r", name)
                return
            if self.on_action:
                try:
                    self.on_action(name)
                except Exception:
                    log.exception("on_action raised")
        elif kind == "H":
            parts = rest.split()
            self.status.firmware = parts[0] if parts else "?"
            self.status.features = set(parts[1].split(",")) if len(parts) > 1 else set()
            self._last_state_sent = ""      # firmware restarted: resend state
            log.info("Core2 firmware %s features=%s",
                     self.status.firmware, ",".join(sorted(self.status.features)) or "-")
        elif kind not in ("P", ""):
            log.debug("core2: %s", line)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v).replace(" ", "_")
