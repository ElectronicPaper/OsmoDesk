"""UDP 9004 datalink: the only transport that actually moves the gimbal.

Sequence, matching OpenPocketCine's DatalinkDriver:

    TCP :7001 poke (stays open)  ->  UDP handshake  ->  ACK  ->  register
    ->  subscribe  ->  40 Hz ACK pump + 1 Hz keepalive  ->  commands

Closing the TCP socket RSTs the camera, so it is held for the whole session
even though no data flows on it after the poke.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

from . import camera, commands, duml, transport
from .duml import Frame

# The firmware calls the link dead after 2 s of silence; so does this side.
LINK_SILENT_S = 2.0

log = logging.getLogger(__name__)

ACK_PUMP_HZ = 40
KEEPALIVE_HZ = 1
# OpenPocketCine starts the DUML counter here rather than at zero.
INITIAL_DUML_SEQ = 0xA000
INBOUND_LOG_LIMIT = 6
# How long to wait for the camera's window ACK before sending commands.
CHANNEL_WAIT_S = 3.0
HANDSHAKE_SENDS = 8
HANDSHAKE_INTERVAL_S = 0.25
# The measured attitude push is ~20 Hz. Ten missed frames is already too old
# for feedback control and long enough to ignore ordinary scheduler jitter.
TELEMETRY_STALE_S = 0.5


class Datalink:
    def __init__(self, host: str = transport.CAMERA_HOST, tcp_poke: bool = True):
        self.host = host
        self.tcp_poke = tcp_poke

        self.sock: socket.socket | None = None
        # When the camera last said anything at all. The link used to have no
        # notion of being dead: attitude is a last-known object that is never
        # cleared, so a camera that went to sleep stayed "connected" forever.
        self.last_rx_at = 0.0
        self.poke_sock: socket.socket | None = None

        self.session_id = 0
        self.base_seq = 0
        self.cam_channel = 0
        self.udp_seq = 0
        self.duml_seq = INITIAL_DUML_SEQ
        self.cmd_counter = 0
        self.ack_windows = transport.AckWindows()

        self._handshake_acked = threading.Event()
        self._channel_seen = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._send_lock = threading.Lock()

        # Latest gimbal attitude from the 0x04/0x05 heartbeat.
        self.attitude = None  # commands.GimbalAttitude | None
        self.power = None  # camera.PowerStatus | None
        self.gimbal_pitch: float | None = None
        self.gimbal_roll: float | None = None
        self.gimbal_yaw: float | None = None
        self.last_attitude_at = 0.0
        self.on_frame = None  # optional callback(Frame)

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        # Every stage acquires something that must survive for a successful
        # session.  A failed handshake used to leave the TCP poke, UDP socket,
        # and receive thread behind until process exit.  Connection retries
        # then accumulated handles and competing readers.
        try:
            self._reset_session()
            if self.tcp_poke:
                self._poke_7001()
            self._open_udp()
            self._threads.append(_spawn(self._rx_loop, "datalink-rx"))

            payload = transport.handshake_payload(self.base_seq)
            for attempt in range(1, HANDSHAKE_SENDS + 1):
                self._send_raw(transport.PKT_HANDSHAKE, payload)
                if self._handshake_acked.wait(HANDSHAKE_INTERVAL_S):
                    break
                log.debug("handshake retry %d/%d", attempt, HANDSHAKE_SENDS)
            if not self._handshake_acked.is_set():
                raise RuntimeError(
                    "camera never answered the datalink handshake -- "
                    "check you are actually associated to the camera SoftAP"
                )
            # Adopt the camera's sequence window before sending anything else.
            if not self._channel_seen.wait(CHANNEL_WAIT_S):
                raise RuntimeError(
                    "camera never advertised a command sequence window -- "
                    "refusing a receive-only link that would silently drop STOP"
                )
            self.udp_seq = (self.cam_channel + 8) & 0xFFFF
            log.info("datalink up, session=0x%04X channel=0x%04X seq=0x%04X",
                     self.session_id, self.cam_channel, self.udp_seq)

            self.send_ack()
            self._register()
            self._subscribe()
            time.sleep(0.4)  # subscribe settle

            self._threads.append(_spawn(self._ack_pump, "datalink-ack"))
            self._threads.append(_spawn(self._keepalive_loop, "datalink-keepalive"))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        for s in (self.sock, self.poke_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.sock = None
        self.poke_sock = None
        self.attitude = None
        self.gimbal_pitch = self.gimbal_roll = self.gimbal_yaw = None
        self.last_attitude_at = 0.0
        log.info("datalink closed")

    def __enter__(self) -> Datalink:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- setup helpers ------------------------------------------------------

    def _reset_session(self) -> None:
        # Ranges and starting values copied from OpenPocketCine's
        # resetHandshakeSession. udp_seq starts at 0, NOT at base_seq: the
        # camera dictates the real command sequence base in its handshake
        # reply, and we adopt it below.
        self.last_rx_at = 0.0
        self.session_id = _rand_between(0x1000, 0xFFFE)
        self.base_seq = _rand_between(0x1000, 0xF000) & 0xFFF8
        self.cam_channel = self.base_seq
        self.udp_seq = 0
        self.duml_seq = INITIAL_DUML_SEQ
        self.cmd_counter = 0
        self.ack_windows = transport.AckWindows.for_handshake(self.base_seq)
        self.video_packets = 0
        self.live = None  # liveview.LiveView | None
        self._inbound_logged = 0
        # Never let a reconnect inherit the last pose from the dead session.
        self.attitude = None
        self.gimbal_pitch = self.gimbal_roll = self.gimbal_yaw = None
        self.last_attitude_at = 0.0
        self._handshake_acked.clear()
        self._channel_seen.clear()
        self._stop.clear()

    def _open_udp(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Ephemeral local port. The camera's 9004 is the *remote*; binding
            # the client to :9004 accepts the handshake then silently drops
            # traffic.
            s.bind(("0.0.0.0", 0))
            s.connect((self.host, transport.CAMERA_UDP_PORT))
            s.settimeout(0.5)
        except BaseException:
            s.close()
            raise
        self.sock = s
        log.debug("UDP %s -> %s:%d", s.getsockname(), self.host, transport.CAMERA_UDP_PORT)

    def _poke_7001(self) -> None:
        # The socket is only handed to self.poke_sock once it is fully set up,
        # so anything that throws before that line -- and connect() to an
        # absent camera times out routinely -- used to drop the socket on the
        # floor still open. close() runs the connect-retry loop, so the leak
        # was one file descriptor per failed attempt, for as long as the
        # operator kept retrying.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(2.0)
            s.connect((self.host, transport.CAMERA_TCP_POKE_PORT))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(duml.encode(commands.set_pairing_pin()))
            time.sleep(0.4)
        except BaseException:
            s.close()
            raise
        self.poke_sock = s  # deliberately kept open for the session
        log.info("TCP 7001 poke ready")

    def _register(self) -> None:
        for build in (commands.app_device_info, commands.app_presence, commands.gimbal_init):
            self.send_frame(build(self._next_duml_seq()))
            self.send_ack()

    def _subscribe(self) -> None:
        sub_id = commands.FIRST_SUB_ID
        for key in commands.SUBSCRIPTION_KEYS:
            self.send_frame(commands.subscribe(key, sub_id, self._next_duml_seq()))
            sub_id += 1
        self.send_ack()

    # -- sending ------------------------------------------------------------

    def _next_duml_seq(self) -> int:
        self.duml_seq = (self.duml_seq + 1) & 0xFFFF
        return self.duml_seq

    def _send_raw(self, pkt_type: int, payload: bytes) -> None:
        if self.sock is None:
            return
        with self._send_lock:
            header = transport.transport_header(pkt_type, len(payload), self.session_id, self.udp_seq)
            try:
                self.sock.send(header + payload)
            except OSError as exc:
                log.warning("udp send failed: %s", exc)
                return
            self.udp_seq = (self.udp_seq + 8) & 0xFFFF

    def send_frame(self, frame: Frame) -> None:
        """Wrap a DUML frame in a routing header and put it on the datalink."""
        if self.sock is None:
            return
        if frame.seq == 0:
            frame.seq = self._next_duml_seq()
        body = duml.encode(frame)
        with self._send_lock:
            self.cmd_counter = (self.cmd_counter + 1) & 0xFF
            routing = transport.routing_header(self.udp_seq, self.cmd_counter)
            header = transport.transport_header(
                transport.PKT_COMMAND, len(routing) + len(body), self.session_id, self.udp_seq
            )
            try:
                self.sock.send(header + routing + body)
            except OSError as exc:
                log.warning("udp send failed: %s", exc)
                return
            self.udp_seq = (self.udp_seq + 8) & 0xFFFF
        log.debug("UDP -> %s", frame)

    def send_ack(self) -> None:
        """Window acknowledgement.

        Always transport seq 0, and it must NOT advance udp_seq. Burning
        sequence numbers on the 40 Hz ACK pump desynchronises every subsequent
        command from what the camera expects.
        """
        if self.sock is None:
            return
        payload = transport.ack_payload(self.ack_windows, self.base_seq)
        header = transport.transport_header(
            transport.PKT_ACK, len(payload), self.session_id, 0)
        with self._send_lock:
            try:
                self.sock.send(header + payload)
            except OSError as exc:
                log.debug("ack send failed: %s", exc)

    # -- receiving ----------------------------------------------------------

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            if self.sock is None:
                self._expire_attitude()
                time.sleep(0.05)
                continue
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                self._expire_attitude()
                continue
            except OSError:
                if self._stop.is_set():
                    return
                self._expire_attitude()
                time.sleep(0.05)
                continue

            self._handle_datagram(data)
            for frame in duml.scan_frames(data):
                self._handle(frame)
            self._expire_attitude()

    @property
    def healthy(self) -> bool:
        """A socket with something recent on it. Mirrors the firmware's own
        2 s silence gate (direct_camera.cpp), so both sides call the same
        link dead at the same moment."""
        return (self.sock is not None and self.last_rx_at > 0.0 and
                time.monotonic() - self.last_rx_at <= LINK_SILENT_S)

    def _handle_datagram(self, data: bytes) -> None:
        """Transport-level bookkeeping, mirroring OpenPocketCine's onDatagram."""
        self.last_rx_at = time.monotonic()
        if self._inbound_logged < INBOUND_LOG_LIMIT:
            self._inbound_logged += 1
            log.info("inbound #%d len=%d pktType=0x%02X  %s",
                     self._inbound_logged, len(data),
                     data[6] if len(data) > 6 else 0xFF, data[:20].hex(" "))

        # The camera's 34-byte telemetry/status datagram contains the command
        # channel and two ACK windows.  Short pktType-0x01 packets are not
        # this shape; accepting their offsets corrupts command and ACK state.
        if len(data) >= 34 and data[6] == transport.PKT_TELEMETRY:
            ch = data[8] | (data[9] << 8)
            if ch:
                self.cam_channel = ch
                self._channel_seen.set()

            # This is a seed for an as-yet unknown reliable-data window only.
            # Once pktType 0x03 establishes it, telemetry must not roll it
            # backwards to a status snapshot.
            if self.ack_windows.acked_data == 0:
                self.ack_windows.acked_data = data[18] | (data[19] << 8)
            self.ack_windows.extra = data[26] | (data[27] << 8)

        if len(data) >= 8 and data[6] == transport.PKT_HANDSHAKE:
            if not self._handshake_acked.is_set():
                log.info("handshake reply: %s", data[:24].hex(" "))
            self._handshake_acked.set()

        # ACK groups are independent.  Taking a cursor from any other packet
        # shape acknowledges data the camera did not send on that stream.
        if len(data) >= 8 and data[6] == transport.PKT_VIDEO:
            self.video_packets += 1
            if self.live is not None:
                self.live.feed_datagram(data)
            seq = transport.transport_seq(data)
            if seq is not None:
                self.ack_windows.video = seq
        elif len(data) >= 8 and data[6] == transport.PKT_ACKED_DATA:
            seq = transport.transport_seq(data)
            if seq is not None:
                self.ack_windows.acked_data = seq

    def _handle(self, frame: Frame) -> None:
        if frame.opcode == (0x04, 0x05):
            att = commands.parse_gimbal_attitude(frame.payload)
            if att is not None:
                self.attitude = att
                self.gimbal_pitch = att.pitch
                self.gimbal_roll = None  # not reported in this frame
                self.gimbal_yaw = att.yaw
                self.last_attitude_at = time.monotonic()
        elif frame.opcode == (0x0D, 0x02):
            got = camera.PowerStatus.parse(frame.payload)
            if got is not None:
                self.power = got
        if self.on_frame is not None:
            try:
                self.on_frame(frame)
            except Exception:
                log.exception("on_frame callback raised")

    def _expire_attitude(self, now: float | None = None) -> None:
        """Erase feedback once it is too old to close a motion loop safely."""
        now = time.monotonic() if now is None else now
        if (self.last_attitude_at and
                now - self.last_attitude_at > TELEMETRY_STALE_S):
            self.attitude = None
            self.gimbal_pitch = self.gimbal_roll = self.gimbal_yaw = None

    # -- background pumps ---------------------------------------------------

    def _ack_pump(self) -> None:
        period = 1.0 / ACK_PUMP_HZ
        while not self._stop.wait(period):
            self.send_ack()

    def _keepalive_loop(self) -> None:
        period = 1.0 / KEEPALIVE_HZ
        while not self._stop.wait(period):
            self.send_frame(commands.app_presence(self._next_duml_seq()))
            self.send_ack()


def _rand_between(low: int, high: int) -> int:
    span = high - low
    return low + (int.from_bytes(os.urandom(2), "little") % span)


def _spawn(target, name: str) -> threading.Thread:
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t
