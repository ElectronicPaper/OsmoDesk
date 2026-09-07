"""BLE leg: discover the camera, app-level pair, read the SoftAP credentials.

BLE carries no gimbal control. Its only job is to hand us the Wi-Fi SSID and
passphrase and to bring the camera's access point up. Everything after that
happens on the UDP datalink (see datalink.py).
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakCharacteristicNotFoundError
from bleak.backends.device import BLEDevice

from . import commands, duml

log = logging.getLogger(__name__)

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"  # notifications + pairing arm
CHAR_WRITE = "0000fff5-0000-1000-8000-00805f9b34fb"  # DUML writes, without response

# DJI company ids seen in the advertisement.
DJI_COMPANY_IDS = (0x08AA, 0xF7AA)

# Advert model ids.
MODEL_IDS = {
    0x20: "Osmo Pocket 3",
    0x21: "Osmo Pocket 4",
    0x22: "Osmo Pocket 4 Pro",
}

# Writes to fff5 are without response and must be paced or they drop.
WRITE_PACING_S = 0.25


def _rediscover_kwargs() -> dict:
    """Backend options that force a fresh GATT service discovery.

    Only Windows caches services across connections, and only bleak's WinRT
    backend accepts the switch -- passing it on Linux raises TypeError, which
    would turn a recoverable stale-cache retry into a hard failure on the Pi.
    BlueZ rediscovers anyway, so there is nothing to ask for there.
    """
    import sys
    return {"winrt": {"use_cached_services": False}} if sys.platform == "win32" else {}


class PairingRejected(RuntimeError):
    pass


class OsmoBle:
    def __init__(self, device: BLEDevice, pin: str = commands.DEFAULT_PIN):
        self.device = device
        self.pin = pin
        self.client = BleakClient(device)
        self._inbox: asyncio.Queue[duml.Frame] = asyncio.Queue()
        self._rx_buf = bytearray()

    # -- discovery ----------------------------------------------------------

    @staticmethod
    async def discover(timeout: float = 15.0, name_hint: str | None = None) -> list[tuple[BLEDevice, str]]:
        """Return [(device, model_name)]. No scan filter -- Pocket 3 omits
        manufacturer data, so we match on name too."""
        found: list[tuple[BLEDevice, str]] = []
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for dev, adv in devices.values():
            model = None
            for cid, raw in (adv.manufacturer_data or {}).items():
                if cid in DJI_COMPANY_IDS:
                    model = _model_from_advert(raw)
                    break
            name = dev.name or ""
            if model is None and not ("Osmo" in name or "DJI" in name):
                continue
            if name_hint and name_hint.lower() not in name.lower():
                continue
            found.append((dev, model or f"unknown ({name})"))
        return found

    # -- session ------------------------------------------------------------

    async def __aenter__(self) -> OsmoBle:
        try:
            await self._open()
        except BleakCharacteristicNotFoundError as exc:
            # Windows keeps its own copy of the peer's GATT table and serves it
            # without consulting the device. When that copy goes stale the
            # characteristics are simply absent -- fff4 disappears and every
            # connection fails from then on, while the camera sits there
            # advertising perfectly happily.
            #
            # Observed directly: an ESP32 enumerated both fff4 and fff5 on this
            # camera minutes after Windows insisted fff4 did not exist. The
            # camera was never the problem.
            #
            # Forcing rediscovery cures it. It is asked for only after a
            # failure rather than on every connect, because it costs a full
            # service walk and the cache is correct nearly all of the time.
            log.warning("GATT cache looks stale (%s) -- rediscovering services",
                        exc)
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = BleakClient(
                self.device, **_rediscover_kwargs())
            await self._open()
        return self

    async def _open(self) -> None:
        await self.client.connect()
        log.info("BLE connected: %s", self.device.address)
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)
        # Arm app-level pairing.
        await self.client.write_gatt_char(CHAR_NOTIFY, b"\x01\x00", response=True)
        await asyncio.sleep(0.2)

    async def __aexit__(self, *exc) -> None:
        try:
            await self.client.stop_notify(CHAR_NOTIFY)
        except Exception:
            pass
        await self.client.disconnect()
        log.info("BLE disconnected")

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._rx_buf += data
        # Frames can span notifications; drain whatever is complete.
        while True:
            start = self._rx_buf.find(0x55)
            if start < 0:
                self._rx_buf.clear()
                return
            if start:
                del self._rx_buf[:start]
            got = duml.decode(bytes(self._rx_buf))
            if got is None:
                if len(self._rx_buf) > 4096:
                    del self._rx_buf[:1]
                    continue
                return
            frame, used = got
            del self._rx_buf[:used]
            log.debug("BLE <- %s", frame)
            self._inbox.put_nowait(frame)

    async def send(self, frame: duml.Frame) -> None:
        log.debug("BLE -> %s", frame)
        await self.client.write_gatt_char(CHAR_WRITE, duml.encode(frame), response=False)
        await asyncio.sleep(WRITE_PACING_S)

    async def request(self, frame: duml.Frame, expect: tuple[int, int],
                      timeout: float = 8.0) -> duml.Frame:
        """Send and wait for a frame with the given (cmd_set, cmd_id)."""
        await self.send(frame)
        return await self.wait_for(expect, timeout=timeout)

    async def wait_for(self, expect: tuple[int, int], timeout: float = 8.0) -> duml.Frame:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no 0x{expect[0]:02X}/0x{expect[1]:02X} within {timeout}s")
            frame = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            if frame.opcode == expect:
                return frame

    # -- the actual flow ----------------------------------------------------

    async def pair(self) -> None:
        """App-level pairing. Replaces Bluetooth bonding."""
        await self.send(commands.session_wake())
        reply = await self.request(commands.set_pairing_pin(self.pin), (0x07, 0x45))
        status = reply.payload[1] if len(reply.payload) > 1 else 0xFF

        if status == 0x01:
            log.info("already paired")
            return
        if status == 0x06:
            # Seen when a second client is already holding the camera. The
            # camera accepts one controller at a time.
            raise PairingRejected(
                "camera refused pairing (0x06) -- another client is probably "
                "already connected; close DJI Mimo or a second panel instance")
        if status != 0x02:
            raise PairingRejected(f"unexpected pairing status: {reply.payload.hex(' ')}")

        log.info("approve the pairing request on the camera screen...")
        approval = await self.wait_for((0x07, 0x46), timeout=60.0)
        await self.send(commands.pair_approval_ack(approval.seq))
        log.info("pairing approved")

    async def read_wifi_credentials(self) -> tuple[str, str]:
        """Bring the AP up, then read SSID and passphrase. Never synthesise them."""
        await self.send(commands.wake_ap())
        await asyncio.sleep(0.5)
        ssid_reply = await self.request(commands.get_wifi_ssid(), (0x07, 0x07))
        pass_reply = await self.request(commands.get_wifi_password(), (0x07, 0x0E))
        ssid = duml.unpack_status_string(ssid_reply.payload)
        password = duml.unpack_status_string(pass_reply.payload)
        if not ssid or not password:
            raise RuntimeError(
                f"empty Wi-Fi credentials (ssid={ssid!r} pass len={len(password)})"
            )
        log.info("camera SoftAP: %s", ssid)
        return ssid, password


def _model_from_advert(raw: bytes) -> str | None:
    """Best-effort model id lookup from the manufacturer payload.

    Bleak strips the 2-byte company id already. The model id byte position is
    firmware dependent, so scan the first few bytes for a known id rather than
    hard-coding an offset.
    """
    for b in raw[:8]:
        if b in MODEL_IDS:
            return MODEL_IDS[b]
    return None
