#!/usr/bin/env python3
"""Drive an Osmo Pocket 4 / 4 Pro gimbal from an M5Stack Core2 IMU.

    python run.py                       # full run: BLE pair, join AP, follow IMU
    python run.py --skip-wifi-join      # already on the camera AP
    python run.py --no-imu --demo       # protocol bring-up without the Core2
    python run.py --dump-telemetry      # print gimbal 0x04/0x05 and stop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from driver import commands, config, transport, wifi
from driver.ble import OsmoBle
from driver.datalink import Datalink
from driver.duml import Frame
from driver.gimbal import AttitudeFollower, GimbalStick
from driver.gimbal import axis as gimbal_axis
from driver.commands import rotation_delta
from driver.imu import ImuLink
from driver.plan import plan_connection

log = logging.getLogger("osmoctl")

CONTROL_HZ = 50


async def obtain_credentials(args) -> tuple[str, str]:
    log.info("scanning for the camera over BLE ...")
    found = await OsmoBle.discover(timeout=args.scan_timeout, name_hint=args.name)
    if not found:
        raise SystemExit("no DJI Osmo camera found -- power it on and keep it near the PC")
    for dev, model in found:
        log.info("  %s  %s  (%s)", dev.address, dev.name or "?", model)
    device, model = found[0]
    log.info("using %s (%s)", device.address, model)

    async with OsmoBle(device, pin=args.pin) as ble:
        await ble.pair()
        return await ble.read_wifi_credentials()


def control_loop(link: Datalink, args) -> None:
    stick = GimbalStick(link, gain=args.gain)
    stick.start()

    if args.demo:
        log.info("demo: pan right, pan left, tilt up, recenter")
        for tilt, pan, label in ((0.0, 0.6, "pan right"), (0.0, -0.6, "pan left"),
                                 (0.6, 0.0, "tilt up")):
            log.info("  %s", label)
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                stick.set_axes(tilt, pan)
                time.sleep(0.05)
            stick.release()
            time.sleep(0.8)
        stick.recenter()
        time.sleep(1.5)
        stick.stop()
        return

    follower = AttitudeFollower(stick, link, kp_tilt=args.kp)
    period = 1.0 / CONTROL_HZ

    with ImuLink(args.imu_port) as imu:
        log.info("hold button A on the Core2 to engage; B recenters, C flips")
        try:
            while True:
                time.sleep(period)
                if imu.take_button_b():
                    stick.recenter()
                if imu.take_button_c():
                    stick.flip()

                s = imu.latest
                if not s.engaged:
                    stick.release()
                    continue

                follower.update(target_pitch_deg=s.pitch * args.tilt_scale,
                                pan_rate_dps=s.yaw_rate)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            stick.release()
            time.sleep(0.2)
            stick.stop()


def dump_telemetry(link: Datalink, seconds: float) -> None:
    log.info("dumping gimbal telemetry for %.0fs -- move the gimbal by hand", seconds)
    seen: set[tuple[int, int]] = set()

    def on_frame(frame):
        if frame.opcode not in seen:
            seen.add(frame.opcode)
            log.info("first 0x%02X/0x%02X  %s", frame.cmd_set, frame.cmd_id,
                     frame.payload[:24].hex(" "))

    link.on_frame = on_frame
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.5)
        log.info("pitch=%s roll=%s yaw=%s",
                 link.gimbal_pitch, link.gimbal_roll, link.gimbal_yaw)


def verify_motion(link: Datalink, args) -> int:
    """Objective check that stick commands actually move the gimbal.

    Stick frames are never acknowledged, so a clean send log proves only that
    packets left the host. This watches the 0x04/0x05 position heartbeat across
    deliberate movements and reports the delta.
    """
    counts: dict[tuple[int, int], int] = {}
    link.on_frame = lambda f: counts.__setitem__(f.opcode, counts.get(f.opcode, 0) + 1)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and link.attitude is None:
        time.sleep(0.2)
    if link.attitude is None:
        log.error("no 0x04/0x05 position heartbeat within 10s")
        log.error("opcodes seen: %s",
                  ", ".join(f"0x{s:02X}/0x{c:02X}" for s, c in sorted(counts)) or "none")
        return 2
    log.info("telemetry alive, quaternion=%s norm=%.4f",
             [round(q, 4) for q in link.attitude.quaternion],
             link.attitude.quaternion_norm)

    if args.live_view:
        # Tests the standing theory that the gimbal ignores stick input until
        # live view is running. Receiver 0x08 is the Pocket variant.
        log.info("enabling live view (0x09/0xa8) ...")
        link.send_frame(commands.live_view_enable())
        time.sleep(2.0)
        log.info("video packets since enable: %d", link.video_packets)

    stick = GimbalStick(link)
    stick.start()
    results = []
    try:
        for label, tilt, pan in (("tilt up", args.deflection, 0.0),
                                 ("tilt down", -args.deflection, 0.0),
                                 ("pan right", 0.0, args.deflection),
                                 ("pan left", 0.0, -args.deflection)):
            before = link.attitude
            end = time.monotonic() + args.hold
            while time.monotonic() < end:
                stick.set_axes(tilt, pan)
                time.sleep(0.04)
            stick.release()
            time.sleep(0.6)
            after = link.attitude
            # Quaternion rotation magnitude: independent of component order,
            # which has not been pinned down. Any real motion shows up here.
            rot = rotation_delta(before, after) if (before and after) else 0.0
            da = (after.pitch - before.pitch) if (before and after) else 0.0
            log.info("%-10s axes=(%d,%d)  rotation=%6.2f deg  angle_a %+.1f -> %+.1f",
                     label, gimbal_axis(tilt), gimbal_axis(pan),
                     rot, before.pitch if before else 0.0,
                     after.pitch if after else 0.0)
            results.append((label, rot, da))
    finally:
        stick.release()
        time.sleep(0.3)
        stick.stop()

    moved = [r for r in results if r[1] > 1.5]
    log.info("")
    if moved:
        log.info("RESULT: gimbal MOVED on %d of %d commands", len(moved), len(results))
        for label, rot, _ in results:
            log.info("  %-10s rotation %6.2f deg", label, rot)
    else:
        log.info("RESULT: no angle change on any command.")
        log.info("Telemetry flows and frames go out, so the link is fine --")
        log.info("the camera is ignoring stick input in this state.")
        if not args.live_view:
            log.info("Next thing to try: --verify-motion --live-view")
    log.info("opcodes seen: %s",
             ", ".join(f"0x{s:02X}/0x{c:02X}({n})" for (s, c), n in sorted(counts.items())))
    return 0


def watch_opcode(link: Datalink, args) -> int:
    """Print an opcode's payload whenever it changes, with the moving bytes marked.

    Derives a payload layout empirically: move the gimbal by hand and whichever
    columns change are the attitude fields. Needed because the 0x04/0x05 layout
    is inherited from Pocket 3 work and was never confirmed on Pocket 4.
    """
    try:
        want_set, want_id = (int(x, 16) for x in args.watch_opcode.split(":"))
    except ValueError:
        log.error("--watch-opcode wants SET:ID in hex, e.g. 04:05")
        return 2

    log.info("watching 0x%02X/0x%02X for %.0fs -- move the gimbal BY HAND now",
             want_set, want_id, args.watch_seconds)
    state = {"last": None, "changed": set(), "count": 0, "first": None}

    def on_frame(frame):
        if frame.opcode != (want_set, want_id):
            return
        state["count"] += 1
        payload = frame.payload
        if state["first"] is None:
            state["first"] = payload
            log.info("len=%d  %s", len(payload), payload.hex(" "))
        prev = state["last"]
        if prev is not None and payload != prev:
            marks = []
            for i in range(min(len(prev), len(payload))):
                if prev[i] != payload[i]:
                    state["changed"].add(i)
                    marks.append(i)
            log.info("%s   changed@ %s", payload.hex(" "),
                     ",".join(str(m) for m in marks))
        state["last"] = payload

    link.on_frame = on_frame
    end = time.monotonic() + args.watch_seconds
    while time.monotonic() < end:
        time.sleep(0.5)

    log.info("")
    log.info("saw %d frames of 0x%02X/0x%02X", state["count"], want_set, want_id)
    if not state["count"]:
        log.info("opcode never arrived.")
        return 0
    varying = sorted(state["changed"])
    log.info("byte offsets that ever changed: %s",
             ", ".join(str(v) for v in varying) if varying else "NONE (payload constant)")
    if varying:
        # Adjacent pairs starting on an even offset are the int16 candidates.
        pairs = [o for o in varying if o + 1 in state["changed"] and o % 2 == 0]
        if pairs:
            log.info("int16-LE field candidates at offsets: %s",
                     ", ".join(str(o) for o in pairs))
        log.info("first payload: %s", state["first"].hex(" "))
        log.info("last  payload: %s", state["last"].hex(" "))
    return 0


def probe_commands(link: Datalink, args) -> int:
    """Send commands that have documented replies and report which come back.

    Separates two very different failures: the camera ignoring stick input
    specifically, versus the datalink discarding every command we send while
    unsolicited telemetry keeps arriving. Only replies prove a write landed.
    """
    seen: dict[tuple[int, int], list] = {}
    link.on_frame = lambda f: seen.setdefault(f.opcode, []).append(f)

    probes = [
        ("gimbal params GET", commands.gimbal_params_get, (0x04, 0x50)),
        ("audio DSP GET", lambda seq=0: Frame(0x02, 0x01, seq, 0x40, 0x02, 0xA0, b""), (0x02, 0xA0)),
        ("tracking poll", lambda seq=0: Frame(0x02, 0x01, seq, 0x40, 0x02, 0xA5, bytes([0])), (0x02, 0xA5)),
        ("live view enable", commands.live_view_enable, (0x09, 0xA8)),
    ]

    results = []
    for label, build, expect in probes:
        before = len(seen.get(expect, []))
        link.send_frame(build())
        deadline = time.monotonic() + 3.0
        got = None
        while time.monotonic() < deadline:
            frames = seen.get(expect, [])
            replies = [f for f in frames[before:] if duml_is_reply(f.flags)]
            if replies:
                got = replies[0]
                break
            time.sleep(0.1)
        if got is not None:
            log.info("%-18s REPLIED flags=0x%02X payload=%s",
                     label, got.flags, got.payload[:16].hex(" ") or "-")
        else:
            log.info("%-18s no reply", label)
        results.append((label, got is not None))

    answered = [r for r in results if r[1]]
    log.info("")
    if answered:
        log.info("RESULT: %d of %d commands answered -- the datalink accepts writes.",
                 len(answered), len(results))
        log.info("So the gimbal stick is being refused specifically, not dropped.")
    else:
        log.info("RESULT: nothing answered. The camera is discarding every command")
        log.info("we send while still pushing telemetry -- the fault is in the")
        log.info("transport/routing layer, not in any individual opcode.")
    log.info("opcodes seen: %s",
             ", ".join(f"0x{s:02X}/0x{c:02X}({len(v)})" for (s, c), v in sorted(seen.items())))
    return 0


def duml_is_reply(flags: int) -> bool:
    return flags in (0xC0, 0x80)


def map_axes(link: Datalink, args) -> int:
    """Work out which reported angle is pitch, roll and yaw.

    Recenters before each move so the deltas are comparable, drives one stick
    axis at a time, and reports how each of the three angle fields responded.
    The field that moves for tilt and stays put for pan is pitch.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and link.attitude is None:
        time.sleep(0.2)
    if link.attitude is None:
        log.error("no 0x04/0x05 heartbeat")
        return 2

    stick = GimbalStick(link)
    stick.start()
    rows = []
    try:
        for label, tilt, pan in (("tilt +", args.deflection, 0.0),
                                 ("tilt -", -args.deflection, 0.0),
                                 ("pan  +", 0.0, args.deflection),
                                 ("pan  -", 0.0, -args.deflection)):
            stick.recenter()
            time.sleep(2.0)
            before = link.attitude
            end = time.monotonic() + args.hold
            while time.monotonic() < end:
                stick.set_axes(tilt, pan)
                time.sleep(0.04)
            stick.release()
            time.sleep(1.0)
            after = link.attitude
            if before is None or after is None:
                continue
            rows.append((
                label,
                _wrap180(after.pitch - before.pitch),
                _wrap180(after.yaw - before.yaw),
                _wrap180(after.yaw_alt - before.yaw_alt),
                rotation_delta(before, after),
            ))
        stick.recenter()
        time.sleep(1.5)
    finally:
        stick.release()
        time.sleep(0.3)
        stick.stop()

    log.info("")
    log.info("%-8s %10s %10s %10s %10s", "move", "pitch", "yaw", "yaw_alt", "rotation")
    for label, da, db, dc, rot in rows:
        log.info("%-8s %+10.1f %+10.1f %+10.1f %10.2f", label, da, db, dc, rot)

    # Whichever field responds to tilt and not to pan is pitch, and vice versa.
    def responds(idx, moves):
        return max(abs(r[idx]) for r in rows if r[0].startswith(moves)) if rows else 0.0

    log.info("")
    for name, idx in (("pitch", 1), ("yaw", 2), ("yaw_alt", 3)):
        t, pn = responds(idx, "tilt"), responds(idx, "pan")
        if t > 2.0 and pn < t / 3:
            verdict = "PITCH (tilt only)"
        elif pn > 2.0 and t < pn / 3:
            verdict = "YAW (pan only)"
        elif t > 2.0 and pn > 2.0:
            verdict = "responds to both -- ambiguous"
        else:
            verdict = "static"
        log.info("%s: tilt=%.1f pan=%.1f  -> %s", name, t, pn, verdict)
    return 0


def _wrap180(delta: float) -> float:
    """Shortest signed difference, so a wrap at +/-180 is not read as a huge jump."""
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    return delta


def hold_pitch(link: Datalink, args) -> int:
    """Closed-loop hold at a target pitch, reporting convergence.

    The end-to-end proof that the control law works on hardware: sign, gain
    and telemetry all have to be right or the error grows instead of shrinking.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and link.attitude is None:
        time.sleep(0.2)
    if link.attitude is None:
        log.error("no 0x04/0x05 heartbeat")
        return 2

    start = link.attitude.pitch
    target = start + args.hold_pitch
    log.info("pitch now %.1f, target %.1f (%+.1f deg), kp=%.3f",
             start, target, args.hold_pitch, args.kp)

    stick = GimbalStick(link)
    stick.start()
    follower = AttitudeFollower(stick, link, kp_tilt=args.kp)
    samples = []
    try:
        end = time.monotonic() + args.hold_seconds
        next_log = 0.0
        while time.monotonic() < end:
            follower.update(target_pitch_deg=target, pan_rate_dps=0.0)
            time.sleep(1.0 / CONTROL_HZ)
            now = time.monotonic()
            err = abs(_wrap180(target - link.attitude.pitch))
            samples.append(err)
            if now >= next_log:
                log.info("  pitch=%+7.1f  error=%6.2f", link.attitude.pitch, err)
                next_log = now + 1.0
    finally:
        stick.release()
        time.sleep(0.3)
        stick.stop()

    first = sum(samples[:10]) / max(1, len(samples[:10]))
    last = sum(samples[-20:]) / max(1, len(samples[-20:]))
    log.info("")
    log.info("initial error %.2f deg -> final error %.2f deg", first, last)
    if last < 2.0:
        log.info("RESULT: CONVERGED. Closed loop holds the target.")
    elif last < first * 0.5:
        log.info("RESULT: converging but not settled -- raise --kp.")
    else:
        log.info("RESULT: did NOT converge. Error grew or stalled.")
        log.info("If the error grew, the tilt sign is inverted.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pin", default=commands.DEFAULT_PIN, help="pairing PIN (default: osmo)")
    ap.add_argument("--name", help="only consider BLE devices whose name contains this")
    ap.add_argument("--scan-timeout", type=float, default=15.0)
    ap.add_argument("--wifi-interface", help='e.g. "Wi-Fi 2" for a second adapter')
    ap.add_argument("--skip-ble", action="store_true",
                    help="skip pairing, requires --ssid/--password or --skip-wifi-join")
    ap.add_argument("--skip-wifi-join", action="store_true",
                    help="already associated to the camera SoftAP")
    ap.add_argument("--ssid")
    ap.add_argument("--password")
    ap.add_argument("--pair-only", action="store_true",
                    help="pair over BLE, print the SoftAP credentials, stop")
    ap.add_argument("--host", default=transport.CAMERA_HOST,
                    help=f"camera address (default {transport.CAMERA_HOST}; "
                         "use the RNDIS address when running over USB)")
    ap.add_argument("--no-tcp-poke", action="store_true")
    ap.add_argument("--imu-port", help="Core2 serial port, e.g. COM7")
    ap.add_argument("--no-imu", action="store_true")
    ap.add_argument("--demo", action="store_true", help="canned move sequence, no IMU")
    ap.add_argument("--hold-pitch", type=float, metavar="DEG",
                    help="closed-loop hold this many degrees from current pitch")
    ap.add_argument("--hold-seconds", type=float, default=8.0)
    ap.add_argument("--map-axes", action="store_true",
                    help="identify which reported angle is pitch/roll/yaw")
    ap.add_argument("--probe-commands", action="store_true",
                    help="send commands with known replies; proves whether writes land")
    ap.add_argument("--watch-opcode", metavar="SET:ID",
                    help="hex opcode to watch, e.g. 04:05; prints payload changes")
    ap.add_argument("--watch-seconds", type=float, default=25.0)
    ap.add_argument("--verify-motion", action="store_true",
                    help="prove stick commands change the reported gimbal angle")
    ap.add_argument("--live-view", action="store_true",
                    help="with --verify-motion: enable live view first")
    ap.add_argument("--hold", type=float, default=1.5,
                    help="with --verify-motion: seconds per move")
    ap.add_argument("--deflection", type=float, default=0.8,
                    help="with --verify-motion: stick throw, 0..1")
    ap.add_argument("--dump-telemetry", type=float, metavar="SECONDS", default=0.0)
    ap.add_argument("--gain", type=float, default=1.0, help="stick output gain")
    ap.add_argument("--kp", type=float, default=0.035, help="tilt proportional gain")
    ap.add_argument("--tilt-scale", type=float, default=1.0,
                    help="IMU pitch -> gimbal pitch ratio")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-restore-wifi", action="store_true",
                    help="stay on the camera AP when the run ends")
    ap.add_argument("--show-config", action="store_true",
                    help="report which credential keys were found (values masked)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Credentials file fills in anything not given on the command line, so the
    # passphrase never has to appear in a shell history or on screen.
    env = config.load()
    args.ssid = args.ssid or config.resolve(env, "ssid")
    args.password = args.password or config.resolve(env, "password")
    args.name = args.name or config.resolve(env, "name")
    args.imu_port = args.imu_port or config.resolve(env, "imu_port")
    args.wifi_interface = args.wifi_interface or config.resolve(env, "wifi_interface")
    if args.pin == commands.DEFAULT_PIN:
        args.pin = config.resolve(env, "pin") or commands.DEFAULT_PIN

    if args.show_config:
        path = config.find_file()
        print(f"  file           : {path if path else '(none found)'}")
        # Key names only. These are not secrets, and knowing them is the whole
        # diagnostic when a value fails to resolve.
        if env:
            known = {k for keys in config.ALIASES.values() for k in keys}
            listed = ", ".join(
                f"{k}{'' if k in known else ' (not recognised)'}" for k in sorted(env)
            )
            print(f"  keys present   : {listed}")
        print(f"  ssid           : {args.ssid or '(unset)'}")
        print(f"  password       : {config.mask(args.password)}")
        print(f"  pin            : {config.mask(args.pin)}")
        print(f"  ble name filter: {args.name or '(none)'}")
        print(f"  wifi interface : {args.wifi_interface or '(default)'}")
        print(f"  imu port       : {args.imu_port or '(autodetect)'}")
        return 0

    plan = plan_connection(
        pair_only=args.pair_only,
        skip_ble=args.skip_ble,
        skip_wifi_join=args.skip_wifi_join,
        ssid=args.ssid,
        password=args.password,
        platform=sys.platform,
    )
    if not plan.ok:
        log.error("%s", plan.error)
        return 2

    ssid, password = args.ssid, args.password
    if plan.need_ble:
        ssid, password = asyncio.run(obtain_credentials(args))

    if plan.stop_after_ble:
        # Write the passphrase straight to the credentials file. Printing it
        # would put it in the terminal scrollback and shell history for the
        # sake of a value the next command can read for itself.
        path = config.save({config.CANONICAL["ssid"]: ssid,
                            config.CANONICAL["password"]: password})
        print()
        print("  SSID     :", ssid)
        print("  password :", config.mask(password), f"-> saved to {path}")
        print()
        print("  next: python run.py --skip-ble --no-imu --demo")
        return 0

    previous_ssid = None
    if plan.need_join:
        # Remember where the adapter was so the machine is not left stranded on
        # a camera access point that has no route to anywhere.
        if not args.no_restore_wifi:
            previous_ssid = wifi.current_ssid(args.wifi_interface)
            if previous_ssid and previous_ssid != ssid:
                log.info("will restore %s when done", previous_ssid)
        wifi.join(ssid, password, interface=args.wifi_interface)

    try:
        return _run_session(args, link_ctx=Datalink(host=args.host, tcp_poke=not args.no_tcp_poke))
    finally:
        if previous_ssid and previous_ssid != ssid:
            log.info("restoring %s ...", previous_ssid)
            if not wifi.reconnect(previous_ssid, args.wifi_interface):
                log.warning("could not rejoin %s -- reconnect manually", previous_ssid)


def _run_session(args, link_ctx) -> int:
    with link_ctx as link:
        if args.hold_pitch is not None:
            return hold_pitch(link, args)
        if args.map_axes:
            return map_axes(link, args)
        if args.probe_commands:
            return probe_commands(link, args)
        if args.watch_opcode:
            return watch_opcode(link, args)
        if args.verify_motion:
            return verify_motion(link, args)
        if args.dump_telemetry > 0:
            dump_telemetry(link, args.dump_telemetry)
            return 0
        if args.no_imu and not args.demo:
            log.info("connected. no IMU, no demo -- idling. ctrl-c to quit")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                return 0
        control_loop(link, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
