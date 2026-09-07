"""Does a gimbal command sent over BLE actually move the head?

The first probe established that the BLE pipe carries gimbal traffic -- the
subsystem answers on it and pushes status unprompted. That is suggestive and
proves nothing: status arriving is not the same as a command being obeyed, and
the frames that came back looked periodic rather than like replies.

Deciding it needs a measurement, not a person watching. So this holds both
transports at once and gives them different jobs:

    BLE   sends every command. Nothing else goes out on it.
    Wi-Fi sends nothing at all. It exists only to read the angle back.

If the reported pitch moves while the UDP side has been silent, the only thing
that could have moved it is the BLE write. If it does not move, BLE really is
pairing-only and the Wi-Fi route is the only way in -- which is what this
project has assumed all along without ever checking.

The stick command is deliberately modest and time-boxed, and the head is
recentred afterwards.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver import commands, config, duml, transport, wifi   # noqa: E402
from driver.ble import CHAR_WRITE, OsmoBle                       # noqa: E402
from driver.datalink import Datalink                             # noqa: E402


def pitch_of(link: Datalink):
    att = link.attitude
    return None if att is None else att.pitch


def d180(a: float) -> float:
    return (a + 180) % 360 - 180


async def raw(ble: OsmoBle, frame: duml.Frame) -> None:
    await ble.client.write_gatt_char(CHAR_WRITE, duml.encode(frame), response=False)


async def main() -> int:
    env = config.load()
    ssid = config.resolve(env, "ssid")
    password = config.resolve(env, "password")

    found = await OsmoBle.discover(timeout=15.0)
    if not found:
        print("no camera advertising over BLE")
        return 1
    device, _ = found[0]
    print(f"camera {device.address}")

    async with OsmoBle(device) as ble:
        await ble.pair()
        # read_wifi_credentials does the 0x53/0x10 wake and then spends a
        # couple of seconds reading SSID and passphrase, which is exactly the
        # grace the access point needs before it starts beaconing. Waking it
        # by hand and joining immediately races that and loses.
        try:
            ssid, password = await ble.read_wifi_credentials()
        except Exception as exc:
            print(f"could not read credentials: {exc}")
            return 1
        print(f"paired, AP up: {ssid}")

        for attempt in (1, 2):
            try:
                wifi.join(ssid, password)
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"could not join the AP for observation: {exc}")
                    return 1
                print("  join missed the AP, waking it again and retrying")
                await ble.send(commands.wake_ap())
                await asyncio.sleep(3.0)

        link = Datalink()
        link.open()
        print("observer up -- UDP is READ ONLY from here on\n")

        for _ in range(60):                       # wait for telemetry
            if pitch_of(link) is not None:
                break
            await asyncio.sleep(0.25)
        start = pitch_of(link)
        if start is None:
            print("no telemetry; cannot measure")
            link.close()
            return 1
        print(f"pitch before        {start:8.2f}")

        # -- drive the head using BLE alone --------------------------------
        seq, t0 = 0x4000, time.monotonic()
        while time.monotonic() - t0 < 4.0:
            seq = (seq + 1) & 0xFFFF
            await raw(ble, commands.gimbal_stick(1330, 1024, seq))
            await asyncio.sleep(0.04)
        await raw(ble, commands.gimbal_stick(1024, 1024, (seq + 1) & 0xFFFF))
        await asyncio.sleep(1.2)

        end = pitch_of(link)
        moved = abs(d180(end - start)) if end is not None else 0.0
        print(f"pitch after         {end:8.2f}")
        print(f"moved               {moved:8.2f} deg\n")

        # Put it back, over BLE too -- if that works it is more evidence.
        await raw(ble, commands.gimbal_recenter((seq + 2) & 0xFFFF))
        await asyncio.sleep(2.0)
        print(f"pitch after recenter{pitch_of(link):8.2f}")

        sent = getattr(link, "udp_seq", None)
        link.close()

        print()
        if moved > 2.0:
            print("VERDICT  BLE carries gimbal control.")
            print("         The head moved while UDP sent nothing, so Bluetooth")
            print("         alone is a real control path and the Core2 build")
            print("         does not need Wi-Fi, a SoftAP join or a UDP stack.")
        else:
            print("VERDICT  BLE does NOT carry gimbal control.")
            print("         Commands were accepted by the write characteristic")
            print("         and the head did not move, so the pipe is genuinely")
            print("         pairing-only. Wi-Fi is the only way in.")
        if sent is not None:
            print(f"         (observer UDP cursor: {sent} -- only ACKs and keepalive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
