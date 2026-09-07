"""Does the camera accept gimbal control over BLE?

The whole project assumes it does not: BLE pairs and hands over Wi-Fi
credentials, and every command that moves the head goes out over the UDP
datalink instead. That assumption was inherited from how the reference
implementations are written, never tested here.

It deserves testing, because DUML is transport agnostic. A frame carries its
own sender and receiver -- a gimbal command is addressed to RX_GIMBAL (0x04)
whichever pipe it travels down -- so there is no structural reason the BLE
write characteristic could not carry one. If it does, the M5Stack Core2 can
drive the camera over Bluetooth alone, with no Wi-Fi, no SoftAP join and no
UDP stack. That is a materially smaller piece of firmware than the Wi-Fi
route, and it is what the operator actually asked for.

Three things are being measured, in order of how much they matter:

  1. Does a discrete command produce a visible action? `recenter` is the test
     case because it is unmistakable -- the head either swings to centre or it
     does not, and no telemetry is needed to tell.
  2. Does anything come back on the notify characteristic? An ACK is proof the
     frame was parsed and routed rather than silently binned.
  3. How fast can frames actually be written? Stick control runs at 25 Hz. The
     driver paces BLE writes at 250 ms because that was safe for pairing, and
     at that rate live control is impossible. Real write-without-response
     throughput on a modern link should be far higher, but "should" is what
     got this assumption made in the first place.

Run with the camera powered and near the PC. Watch the head.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from driver import commands, duml               # noqa: E402
from driver.ble import CHAR_WRITE, OsmoBle      # noqa: E402


async def raw_write(ble: OsmoBle, frame: duml.Frame) -> None:
    """Write with no pacing, so throughput can be measured honestly."""
    await ble.client.write_gatt_char(CHAR_WRITE, duml.encode(frame), response=False)


async def drain(ble: OsmoBle, seconds: float) -> list[duml.Frame]:
    """Collect whatever the camera says back."""
    out: list[duml.Frame] = []
    end = asyncio.get_running_loop().time() + seconds
    while True:
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            return out
        try:
            out.append(await asyncio.wait_for(ble._inbox.get(), timeout=remaining))
        except (asyncio.TimeoutError, TimeoutError):
            return out


async def main() -> int:
    found = await OsmoBle.discover(timeout=15.0)
    if not found:
        print("no camera advertising over BLE -- power it on and keep it close")
        return 1
    device, model = found[0]
    print(f"camera: {device.address}  {model}\n")

    async with OsmoBle(device) as ble:
        await ble.pair()
        print("paired\n")

        # -- 1. a discrete, visible command ---------------------------------
        print("TEST 1  recenter over BLE  (watch the head)")
        seq = 0x3000
        await raw_write(ble, commands.gimbal_recenter(seq))
        replies = await drain(ble, 2.5)
        print(f"        {len(replies)} frame(s) came back")
        for f in replies:
            print(f"          {f.opcode[0]:#04x}/{f.opcode[1]:#04x} "
                  f"payload={f.payload.hex(' ')[:48]}")
        gimbal_replies = [f for f in replies if f.opcode[0] == 0x04]
        print("        gimbal subsystem answered"
              if gimbal_replies else
              "        nothing from the gimbal subsystem")

        # -- 2. sustained stick, the thing live control needs ---------------
        print("\nTEST 2  stick over BLE for 3 s  (watch for a slow tilt)")
        sent, t0 = 0, time.monotonic()
        while time.monotonic() - t0 < 3.0:
            seq = (seq + 1) & 0xFFFF
            # A modest, unambiguous tilt: centre 1024, so this is well clear of
            # the dead band but nowhere near a travel stop.
            await raw_write(ble, commands.gimbal_stick(1330, 1024, seq))
            sent += 1
            await asyncio.sleep(0.04)          # the real 25 Hz cadence
        elapsed = time.monotonic() - t0
        await raw_write(ble, commands.gimbal_stick(1024, 1024, seq + 1))
        print(f"        {sent} frames in {elapsed:.1f}s = {sent / elapsed:.0f} Hz")

        # -- 3. is telemetry available over this pipe too? ------------------
        print("\nTEST 3  listening 3 s for unsolicited telemetry")
        tel = await drain(ble, 3.0)
        kinds = sorted({f"{f.opcode[0]:#04x}/{f.opcode[1]:#04x}" for f in tel})
        print(f"        {len(tel)} frame(s), opcodes: {kinds or 'none'}")

    print("\n--- verdict rests on what the head did ---")
    print("moved on TEST 1 or 2 -> BLE carries control, and Core2-direct over")
    print("                        Bluetooth alone is on the table")
    print("no movement at all   -> BLE really is pairing-only, and the Wi-Fi")
    print("                        route is the only way in")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
