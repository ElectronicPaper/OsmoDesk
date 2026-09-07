#!/usr/bin/env python3
"""Local web control panel for the Osmo Pocket 4 / 4 Pro.

    .venv\\Scripts\\python.exe server.py --wifi-interface "Wi-Fi"

Then open http://127.0.0.1:8722 in a browser.

Runs on the same machine as the driver because the camera link is a private
Wi-Fi association plus a UDP session held open by this process. The page is
served from here rather than published anywhere: a hosted page cannot reach a
localhost driver.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import secrets
import socket
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit

from driver import (camera, census, commands, config, library, lut, moves,
                    pathexport, preflight, repeatability, shutter,
                    timelapse, transport, wifi)
from driver.datalink import Datalink
from driver.gimbal import GimbalStick, MoveRunner, TILT_SIGN, YAW_SIGN
from driver.liveview import LiveView
from driver.clutch import Clutch, GAINS
from driver.limits import LimitMonitor
from driver import response, shaping
from driver.core2 import Core2Link
from driver import moves
from run import obtain_credentials

log = logging.getLogger("panel")

WEB_DIR = Path(__file__).resolve().parent / "web"
# Beside the code rather than in a temp folder: a shot library that does not
# survive a reboot is not a shot library.
MOVES_DIR = Path(__file__).resolve().parent / "moves"

# How often rig state is pushed to the Core2. This thread competes with the
# HEVC decoder for the GIL, and preview is the thing that suffers.
CORE2_FEED_PERIOD_S = 0.25
MAX_JSON_BODY_BYTES = 64 * 1024


def _step10(v: int) -> int:
    """Round to a tenth. The Core2 redraws on every changed state line, and
    four ends reporting raw 0..100 would repaint the box throughout a move for
    changes far below what a 320x240 bar or a haptic pulse can express."""
    return int(round(v / 10.0)) * 10


def _strict_bool(value, setting: str) -> bool:
    """Accept JSON booleans only for externally supplied flags.

    Python's ``bool('false')`` is true, so coercing HTTP input silently turns
    a typo or a form value into an exposure/focus change.
    """
    if type(value) is not bool:
        raise ValueError(f"{setting} must be a boolean")
    return value


class CameraSession:
    """Owns the camera connection and the stick pump for the whole process."""

    def __init__(self, args):
        self.args = args
        self.link: Datalink | None = None
        self.stick: GimbalStick | None = None
        self.state = "idle"
        self.stage = ""
        self.error: str | None = None
        self.ssid: str | None = None
        self.move = moves.Move(name="untitled")
        self.library = library.Library(MOVES_DIR)
        # Passive observer on the inbound frames. The driver subscribes to five
        # camera status streams and decodes none of them, which is the reason
        # the monitor cannot prove the camera is recording. This is how those
        # get read: capture a session, mark it while pressing record, and see
        # which byte flips.
        self.census = census.FrameCensus()
        # Last state the box was cued about, so cues fire on the EDGE. Sending
        # "go" every tick would beep continuously for the length of the move.
        self._cued = {"armed": False, "moving": False}
        self.tl_state: dict = {"running": False, "frame": 0, "frames": 0,
                               "error": None, "plan": None}
        self._tl_stop = threading.Event()
        self._tl_thread: threading.Thread | None = None
        # A viewing look, not a recorded one. Held on the session so the
        # monitor and the panel cannot disagree about which look is up.
        self.lut = None
        self.lut_name = ""
        self.runner: MoveRunner | None = None
        self.limits = moves.SoftLimits()
        self.live: LiveView | None = None
        self.armed = False
        self.disarm_reason = "never armed"
        # One motion owner at a time. "Last packet wins" is not acceptable when
        # the thing being arbitrated physically moves.
        self.owner = "none"          # none | program | core2 | phone
        self.clutch = Clutch(limits=self.limits)
        self.limit_monitor = LimitMonitor()
        self.speed_preset = "normal"
        self.ramp = shaping.DEFAULT_RAMP
        self.tilt_stability = "balanced"
        self.pan_stability = "balanced"
        # What we have asked the camera for. Requests, not readings.
        self.camera_state: dict = {}
        # Shutter angle only means anything against a frame rate. Nothing on
        # this camera reports its frame rate back over any command implemented
        # here, so this is the rate the controller last set -- or the film
        # default until it sets one. Shown to the operator as an assumption,
        # never as a measurement.
        self.mains_hz: float = 50.0
        self.recording = False
        # Pre-roll: seconds between "roll camera" and the move starting.
        self.preroll_s = 3.0
        self.preroll_until = 0.0
        self._roll_timer: threading.Timer | None = None
        self.recording_since: float | None = None
        # Whether a hand rotation raises or lowers the frame is a
        # preference, so the box owns the switch and this side applies it.
        self.invert_tilt = False
        self.invert_pan = False
        self.core2: Core2Link | None = None
        self.fault = ""
        self.imu_profile = "FLAT"
        self._kinetic_thread: threading.Thread | None = None
        self._kinetic_stop = threading.Event()
        self.takes: list[dict] = []
        self.slate = {"scene": "1", "shot": "A", "take": 1}
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            if self.state in ("connecting", "connected"):
                return
            self.state = "connecting"
            self.stage = "starting"
            self.error = None
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        # BaseException, not Exception: obtain_credentials raises SystemExit
        # when no camera is found, which would otherwise kill this thread
        # silently and leave the UI stuck on "connecting" forever.
        self.armed = False
        self.disarm_reason = "connecting -- arm before playing a move"
        try:
            ssid, password = self.args.ssid, self.args.password
            if not self.args.skip_ble:
                # BLE also sends the 0x53/0x10 AP wake; the SoftAP sleeps
                # whenever no client holds it.
                self.stage = "pairing over BLE"
                ssid, password = asyncio.run(obtain_credentials(self.args))
            self.ssid = ssid
            if not self.args.skip_wifi_join:
                self._join_ap(ssid, password)

            self.stage = "opening datalink"
            link = Datalink(host=self.args.host)
            # Attached BEFORE open() so the subscription pushes that arrive
            # during registration are counted too -- those are the frames most
            # likely to carry record state and card space.
            link.on_frame = self.census.note
            link.open()

            # Live view before anything else touches the camera: the decoder
            # has to be listening when the first keyframe arrives.
            if not self.args.no_live_view:
                self.stage = "starting live view"
                live = LiveView()
                live.on_need_keyframe = self._request_keyframe
                live.start()
                link.live = live
                self.live = live
                link.send_frame(commands.live_view_enable())
            stick = GimbalStick(link, gain=self.args.gain)
            stick.start()
            runner = MoveRunner(link, stick, kp=self.args.kp, limits=self.limits)
            with self._lock:
                self.link, self.stick, self.runner = link, stick, runner
                stick.set_speed_cap(
                    response.SPEED_CAPS.get(self.speed_preset,
                                            response.SPEED_CAPS["normal"]))
                stick.set_ramp(self.ramp)
                stick.set_axis_stability(self.tilt_stability,
                                          self.pan_stability)
                # Still disarmed: set at the top of this method so a FAILED
                # connect cannot leave a previously-armed rig live either.
                self.disarm_reason = "connected -- arm before playing a move"
                self.state, self.stage = "connected", ""
            log.info("camera session up")
        except BaseException as exc:
            log.exception("connect failed")
            with self._lock:
                self.state = "error"
                self.stage = ""
                self.error = f"{type(exc).__name__}: {exc}"

    def disconnect(self) -> None:
        with self._lock:
            stick, link, runner, live = self.stick, self.link, self.runner, self.live
            self.stick = self.link = self.runner = self.live = None
            self.state = "idle"
        # The Core2 deliberately survives a camera disconnect. It is a separate
        # device on a separate cable, and unplugging the camera is no reason to
        # drop a controller the operator is still holding. Its own supervisor
        # handles the cable coming and going.
        self._kinetic_stop.set()
        self.limit_monitor.reset()
        if live:
            live.stop()
        if runner:
            runner.stop()
        if stick:
            stick.release()
            time.sleep(0.2)
            stick.stop()
        if link:
            link.close()

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        link, stick = self.link, self.stick
        out = {
            "state": self.state,
            "stage": self.stage,
            "error": self.error,
            "ssid": self.ssid,
            "host": self.args.host,
        }
        if link is not None:
            att = link.attitude
            out.update({
                "session": f"0x{link.session_id:04X}",
                "channel": f"0x{link.cam_channel:04X}",
                "video_packets": link.video_packets,
                "pitch": att.pitch if att else None,
                "yaw": att.yaw if att else None,
                "quaternion": [round(q, 4) for q in att.quaternion] if att else None,
                "telemetry": att is not None,
            })
        if link is not None and link.power is not None:
            out["power"] = {
                "percent": link.power.percent,
                "charging": link.power.charging,
                "milliamps": link.power.milliamps,
            }
        out["live"] = self.live.stats() if self.live is not None else {"running": False}
        out["owner"] = self.owner
        out["fault"] = self.fault
        out["clutch"] = self.clutch.state.to_dict()
        out["clutch"]["gain"] = self.clutch.gain_name
        out["axis_tune"] = {
            "tilt_response": self.clutch.tilt_gain_name,
            "pan_response": self.clutch.pan_gain_name,
            "tilt_stability": self.tilt_stability,
            "pan_stability": self.pan_stability,
        }
        limits = self._limit_report()
        # Kept for the Core2 haptics, which need one figure to pick a pulse
        # strength. Nothing visual reads it.
        out["clutch"]["proximity"] = limits["worst"]
        out["limit_axes"] = limits
        # The travel geometry itself, so the panel can DRAW the arcs rather
        # than describe them. It never changes at runtime, but sending it with
        # the status is cheaper than another endpoint and means the picker can
        # never be drawing last week's calibration.
        out["timelapse"] = dict(self.tl_state)
        out["start_check"] = self.start_check()
        out["travel"] = {
            "pitch": {"start": moves.PITCH_LIMITS.start,
                      "span": moves.PITCH_LIMITS.usable_span},
            "yaw": {"start": moves.YAW_LIMITS.start,
                    "span": moves.YAW_LIMITS.usable_span},
        }
        out["imu_profile"] = self.imu_profile
        out["speed_preset"] = self.speed_preset
        out["ramp"] = self.ramp
        # stop_time is what the operator actually cares about: how long the
        # head keeps moving after they let go.
        out["ramps"] = [{"name": r.name, "blurb": r.blurb,
                         "stop_time": round(r.stop_time, 2)}
                        for r in shaping.RAMPS.values()]
        out["invert"] = {"tilt": self.invert_tilt, "pan": self.invert_pan}
        # `reported` is the important field: everything in here is what this
        # panel last asked for, and nothing in the implemented protocol reads
        # the camera's own exposure or tally back.
        out["camera"] = dict(self.camera_state, reported=False)
        out["shutter"] = self.shutter_info()
        out["link_healthy"] = bool(self.link is not None and
                                   getattr(self.link, "healthy", True))
        out["preroll"] = {"seconds": self.preroll_s,
                          "until": self.preroll_until}
        out["recording"] = {"on": self.recording, "reported": False,
                            "since": self.recording_since}
        out["core2"] = self.core2.status.to_dict() if self.core2 else {"connected": False}
        if self.link is not None and self.link.attitude is not None:
            up, dn = self.clutch.headroom(self.link.attitude.pitch)
            out["headroom"] = {"up": up, "down": dn}
        out["armed"] = self.armed
        out["disarm_reason"] = self.disarm_reason
        out["setup"] = dict(self.move.setup)
        out["slate"] = dict(self.slate)
        out["takes"] = self.takes[-12:]
        out["take_count"] = len(self.takes)
        r = self.runner
        out["move"] = {
            "name": self.move.name,
            "loop": self.move.loop,
            "ping_pong": self.move.ping_pong,
            "waypoints": [w.to_dict() for w in self.move.waypoints],
            "total_duration": round(self.move.total_duration, 2),
            "running": bool(r and r.running),
            "elapsed": round(r.elapsed, 2) if r else 0.0,
            "progress": round(r.progress, 4) if r else 0.0,
            "error_pitch": round(r.error_pitch, 2) if r else 0.0,
            "error_yaw": round(r.error_yaw, 2) if r else 0.0,
            "clamped": bool(r and r.clamped),
            "waiting_cue": r.waiting_cue if r else None,
            "cues": self.move.cue_points(),
            "report": r.report.to_dict() if r else None,
        }
        out["limits"] = {"start": round(self.limits.start, 1),
                         "end": round(self.limits.end, 1),
                         "span": round(self.limits.usable_span, 1)}
        return out

    # -- actions ------------------------------------------------------------

    def require(self) -> tuple[Datalink, GimbalStick]:
        if self.link is None or self.stick is None:
            raise RuntimeError("not connected")
        return self.link, self.stick

    def set_axes(self, tilt: float, pan: float) -> None:
        """Manual jog. Takes motion authority away from any running program.

        One authority at a time: if an operator grabs the stick, the programmed
        move is abandoned and the rig latches disarmed. Half-manual,
        half-automatic motion is how someone gets hit.
        """
        _, stick = self.require()
        if self.runner is not None and self.runner.running:
            self.runner.stop(aborted=True)
            self.disarm("manual takeover")
        elif self.armed and (tilt or pan):
            self.disarm("manual takeover")
        if tilt or pan:
            self.owner = "phone"
        # Intent feeds the limit warning as well as the head: a full-deflection
        # command toward a stop should light the indicator now, not a quarter
        # second later once the rate estimate has caught up.
        # The monitor reasons in the telemetry frame too -- positive is toward
        # the high end of the arc -- so operator input has to be converted or
        # the warning lights the opposite side of the dial from the stop being
        # approached.
        self.limit_monitor.command(tilt / TILT_SIGN, pan / YAW_SIGN)
        stick.set_axes(tilt, pan)

    def _join_ap(self, ssid: str, password: str) -> None:
        """Join the camera access point, waking it again if it is not up yet.

        The wake is one BLE write and the camera answers it in its own time.
        Sometimes the point is beaconing before the join starts scanning and
        sometimes it is not, and a single attempt turns that race into a failed
        connect -- observed repeatedly, always recovering on a second try.

        So a miss re-sends the wake over a fresh BLE session and scans again
        rather than abandoning the whole connect. The BLE leg is redone because
        the first one has already been closed by the time the join runs, and
        the wake has to come from somewhere.
        """
        attempts = 1 if self.args.skip_ble else 3
        for attempt in range(1, attempts + 1):
            self.stage = (f"joining {ssid}" if attempt == 1
                          else f"joining {ssid} (attempt {attempt})")
            try:
                wifi.join(ssid, password,
                              interface=self.args.wifi_interface)
                return
            except Exception as exc:
                if attempt == attempts:
                    raise
                log.info("access point not up yet (%s) -- waking it again", exc)
                self.stage = "waking the access point"
                try:
                    asyncio.run(obtain_credentials(self.args))
                except Exception as wake_exc:
                    # If the wake itself fails there is nothing left to try,
                    # and the original join error is the more useful one.
                    log.info("could not re-wake over BLE: %s", wake_exc)
                    raise exc from wake_exc

    def attach_core2(self) -> None:
        """Start supervising the hand controller, independently of the camera.

        This used to run inside connect(), so a camera that failed to pair --
        which on this rig is a routine BLE timeout -- also left the Core2
        unattached even though it was plugged in and streaming. The link
        supervises its own port, so starting it once at boot is both simpler
        and more robust than tying it to an unrelated device's session.
        """
        if self.core2 is not None:
            return
        if self.args.no_core2 and not self.args.imu_port:
            return
        try:
            self._attach_core2()
        except Exception as exc:
            log.info("no Core2 attached (%s)", exc)

    def _attach_core2(self) -> None:
        link = Core2Link(self.args.imu_port)
        link.on_action = self._core2_action
        link.on_engage = self._core2_engage
        link.on_jog = self._core2_jog
        link.start()
        self.core2 = link
        threading.Thread(target=self._core2_feed, name="core2-feed",
                         daemon=True).start()

    def _core2_action(self, name: str) -> None:
        try:
            if name == "abort":
                self.stop_everything("STOP from Core2")
            elif name == "go":
                self.cue_go()
            elif name == "arm":
                self.arm()
            elif name == "disarm":
                self.disarm("Core2")
            elif name == "rec":
                self.action("record_start")
            elif name == "record_start":
                self.action("record_start")
            elif name == "record_stop":
                self.action("record_stop")
            elif name == "tl_stop":
                self.timelapse_stop()
            elif name == "feel_defaults":
                # The profile is persisted on the box, but a USB host owns the
                # active controller. Mirror the same defaults or its next
                # state packet would silently undo the visible reset.
                self.clutch.set_gain("normal")
                self.set_speed("normal")
                self.set_ramp(shaping.DEFAULT_RAMP)
                self.set_axis_stability("tilt", "balanced")
                self.set_axis_stability("pan", "balanced")
                self.invert_tilt = False
                self.invert_pan = False
            elif name == "recenter":
                self.action("recenter")
            elif name == "tilt_lock":
                self.clutch.set_locks(tilt=not self.clutch.state.lock_tilt)
            elif name == "pan_lock":
                self.clutch.set_locks(pan=not self.clutch.state.lock_pan)
            elif name == "gain_next":
                order = sorted(GAINS, key=lambda k: GAINS[k])
                nxt = order[(order.index(self.clutch.gain_name) + 1) % len(order)]
                self.clutch.set_gain(nxt)
            elif name.startswith("tilt_response_") or name.startswith("pan_response_"):
                axis, label = name.split("_response_", 1)
                response_name = {
                    "FINE": "fine", "BALANCED": "normal", "DIRECT": "fast"
                }.get(label)
                if response_name is None:
                    raise ValueError(f"unknown {axis} response {label!r}")
                self.clutch.set_axis_response(**{axis: response_name})
            elif name.startswith("tilt_stability_") or name.startswith("pan_stability_"):
                axis, label = name.split("_stability_", 1)
                self.set_axis_stability(axis, label.lower())
            elif name.startswith("profile_"):
                self.imu_profile = name.split("_", 1)[1]
            elif name == "reconnect":
                # From the box's Wi-Fi page. It cannot see the network itself,
                # so it asks the host to redo the join rather than pretending
                # to know anything about the link.
                self.connect()
            elif name.startswith("limitleds_") or name.startswith("beacon_"):
                pass                      # the box owns its own lights
            elif name.startswith("speed_") or name.startswith("jog_speed_"):
                # The box names the preset; this side owns what it means in
                # degrees per second, so the two can never drift apart. One
                # stick here, so jog and hand speed are the same setting.
                self.set_speed({"SLOW": "fine", "NORMAL": "normal",
                                "FAST": "fast"}.get(name.rsplit("_", 1)[1],
                                                    "normal"))
            elif name.startswith("smooth_") or name.startswith("jog_smooth_"):
                self.set_ramp({"CRISP": "news", "FLUID": "fluid",
                               "GLIDE": "glide"}.get(name.rsplit("_", 1)[1],
                                                     shaping.DEFAULT_RAMP))
            elif name.startswith("template_"):
                pass                      # no host-side rate template yet
            elif name.startswith("invert_tilt_"):
                self.invert_tilt = name.endswith("1")
            elif name.startswith("invert_pan_"):
                self.invert_pan = name.endswith("1")
        except Exception as exc:
            log.info("core2 action %s refused: %s", name, exc)

    def _core2_jog(self, tilt: float, pan: float) -> None:
        """Jog pad on the box. Same ownership rules as the phone's stick."""
        try:
            if tilt or pan:
                self.take_ownership("core2")
            self.set_axes(tilt, pan)
        except Exception as exc:
            log.debug("core2 jog refused: %s", exc)

    def _core2_engage(self, engaged: bool) -> None:
        try:
            if engaged:
                self.grab("core2")
            else:
                self.let_go("core2")
        except Exception as exc:
            log.info("core2 clutch refused: %s", exc)
            self.fault = str(exc)

    def set_ramp(self, name: str) -> None:
        """Ease preset for live control. Unknown names fall back rather than
        raise: a typo from a client must never leave the operator without a
        working stick."""
        if name not in shaping.RAMPS:
            name = shaping.DEFAULT_RAMP
        self.ramp = name
        if self.stick is not None:
            self.stick.set_ramp(name)

    def set_speed(self, preset: str) -> None:
        """Top stick speed, by name. Unknown names fall back to normal rather
        than raising: a typo from a client must not leave the operator with no
        working control."""
        cap = response.SPEED_CAPS.get(preset, response.SPEED_CAPS["normal"])
        self.speed_preset = preset if preset in response.SPEED_CAPS else "normal"
        if self.stick is not None:
            self.stick.set_speed_cap(cap)

    def set_axis_stability(self, axis: str, name: str) -> None:
        """Set one axis' weight while preserving the other axis and ramp."""
        if axis not in ("tilt", "pan"):
            raise ValueError(f"unknown axis {axis!r}")
        if name not in shaping.STABILITY_FACTORS:
            raise ValueError(f"unknown axis stability {name!r}")
        setattr(self, f"{axis}_stability", name)
        if self.stick is not None:
            self.stick.set_axis_stability(self.tilt_stability,
                                          self.pan_stability)

    def _set_recording(self, on: bool) -> None:
        """Note that a record command went out.

        The record opcode is unverified on this camera and nothing reports the
        tally back, so this is belief, not knowledge. It is published with
        `reported: False` so a monitor can show the difference rather than
        painting a confident red border over a camera that may not be rolling.
        """
        self.recording = on
        self.recording_since = time.time() if on else None

    def _request_keyframe(self) -> None:
        """Nudge the camera into emitting a fresh IDR.

        Re-sending the live-view enable makes it restart its GOP. Without this
        a stalled decoder waits for a random-access point that, on this
        camera, arrives roughly twice a session.
        """
        link = self.link
        if link is not None:
            link.send_frame(commands.live_view_enable())

    def _limit_report(self) -> dict:
        """Per-axis, per-direction closeness to a stop.

        Replaces a single 0..100 for the whole rig. That number could only
        colour the entire control, and it was computed against a quarter of
        total travel -- 38 degrees on the pitch arc -- so a head sitting at
        rest already read as near a limit. See driver/limits.py.
        """
        link = self.link
        att = link.attitude if link else None
        if att is not None:
            self.limit_monitor.feed(att.pitch, att.yaw)
        return self.limit_monitor.report()

    def cue(self, name: str) -> None:
        """Tell the box to make a noise. Fire and forget.

        The crew has to know the head is about to move without looking at a
        screen -- the operator is watching the subject and the AC is watching
        the lens.
        """
        box = self.core2
        if box is not None and name in ("arm", "count", "go", "end"):
            try:
                box.send_state(rcue=name)
            except Exception:                    # noqa: BLE001
                log.debug("cue %s did not reach the box", name, exc_info=True)

    def _cue_edges(self, armed: bool, moving: bool) -> None:
        """Cue on transitions only.

        Level-driven cues would beep on every feed tick for the whole length
        of a move, which is how a useful sound becomes one the crew learns to
        ignore.
        """
        if armed and not self._cued["armed"]:
            self.cue("arm")
        if moving and not self._cued["moving"]:
            self.cue("go")
        elif self._cued["moving"] and not moving:
            self.cue("end")
        self._cued = {"armed": armed, "moving": moving}

    def _core2_feed(self) -> None:
        """Push rig state down to the box so its beacon and screen are true.

        Deliberately slow. `send_state` already suppresses unchanged lines, but
        this loop still wakes, builds a dict and formats a string every tick,
        and it shares the GIL with the HEVC decode thread. At 10 Hz that was
        enough to starve the decoder down to a single frame; the beacon and a
        state word do not need to update faster than a human can look at them.
        """
        while self.core2 is not None:
            time.sleep(CORE2_FEED_PERIOD_S)
            c, link, r = self.core2, self.link, self.runner
            if c is None:
                return
            att = link.attitude if link else None
            # attitude is last-known and never cleared; a sleeping camera
            # would keep "tel" true forever. The datalink's own silence gate
            # decides whether that attitude is worth anything.
            if att is not None and not getattr(link, "healthy", True):
                att = None
            up, dn = self.clutch.headroom(att.pitch) if att else (0.0, 0.0)
            lim = self._limit_report()
            # Recording is only ever asserted when it can be proven. The record
            # opcode is unverified on this camera, so the tally stays dark.
            self._cue_edges(bool(self.armed), bool(r and r.running))
            c.send_state(
                cam=bool(link), armed=self.armed,
                moving=bool(r and r.running), rec=False,
                # Intent, distinct from the proven tally: the REC key on the
                # box could start a recording over USB but never stop one.
                recint=self.recording,
                tel=att is not None,
                cue=(r.waiting_cue if (r and r.waiting_cue is not None) else -1),
                owner=self.owner, move=(self.move.name or ""),
                t=(r.elapsed if r else 0.0),
                total=self.move.total_duration,
                up=up, dn=dn,
                limit=self.clutch.state.near_limit,
                near=_step10(lim["worst"]),
                ptl=_step10(lim["pitch"]["low"]),
                pth=_step10(lim["pitch"]["high"]),
                ywl=_step10(lim["yaw"]["low"]),
                ywh=_step10(lim["yaw"]["high"]),
                # A timelapse runs unattended for hours; the operator will be
                # somewhere else in the room, not at the browser. Zero when
                # none is running, which the box reads as "show the page name".
                tlf=(self.tl_state["frame"] if self.tl_state["running"] else 0),
                tln=(self.tl_state["frames"] if self.tl_state["running"] else 0),
                ssid=(self.ssid or ""),
                lockt=self.clutch.state.lock_tilt,
                lockp=self.clutch.state.lock_pan,
                gain=self.clutch.gain_name,
                trsp=self.clutch.tilt_gain_name,
                prsp=self.clutch.pan_gain_name,
                tstab=self.tilt_stability,
                pstab=self.pan_stability,
                speed=self.speed_preset,
                invt=self.invert_tilt,
                invp=self.invert_pan,
                fault=self.fault or "",
            )
            # Attitude is the only genuinely per-frame value, and the box only
            # renders headroom rails from it. Quarter rate is plenty.
            self._attitude_tick = (getattr(self, "_attitude_tick", 0) + 1) % 4
            if att and self._attitude_tick == 0:
                c.send_attitude(att.pitch, att.yaw)

    # -- control ownership ----------------------------------------------------

    def take_ownership(self, who: str) -> None:
        """Claim motion authority, pre-empting whatever held it.

        The physical clutch outranks everything: someone with the box in their
        hand and their eyes on the shot must never be overridden by a phone.
        """
        if self.owner == "core2" and who == "phone" and self.clutch.state.engaged:
            raise RuntimeError("the Core2 clutch is held -- release it first")
        if self.owner == "program" and who != "program":
            if self.runner is not None and self.runner.running:
                self.runner.stop(aborted=True)
            self.disarm(f"{who} took control")
        self.owner = who

    def release_ownership(self, who: str) -> None:
        if self.owner == who:
            self.owner = "none"

    # -- kinetic clutch -------------------------------------------------------

    def grab(self, who: str = "phone") -> None:
        """Take the head from wherever it is. The frame must not jump."""
        link, _ = self.require()
        att = link.attitude
        if att is None:
            raise RuntimeError("no telemetry -- cannot grab safely")
        imu = self.core2.latest.pitch if self.core2 else 0.0
        self.take_ownership(who)
        self.clutch.engage(imu_pitch=imu, gimbal_pitch=att.pitch, gimbal_yaw=att.yaw)
        self._start_kinetic()

    def let_go(self, who: str = "phone") -> None:
        self.clutch.release()
        self.release_ownership(who)

    def _start_kinetic(self) -> None:
        if self._kinetic_thread is not None and self._kinetic_thread.is_alive():
            return
        self._kinetic_stop.clear()
        self._kinetic_thread = threading.Thread(
            target=self._kinetic_loop, name="clutch", daemon=True)
        self._kinetic_thread.start()

    def _kinetic_loop(self) -> None:
        """Drive the head from the controller while the clutch is active."""

        period = 1.0 / 40.0
        kp = self.args.kp
        while not self._kinetic_stop.wait(period):
            if not self.clutch.active:
                break
            link, stick = self.link, self.stick
            if link is None or stick is None:
                break
            att = link.attitude
            if att is None:
                self.clutch.abort()       # no feedback: stop, do not guess
                break
            imu = self.core2.latest if self.core2 else None
            target = self.clutch.target(imu.pitch if imu else 0.0,
                                        imu.yaw_rate if imu else 0.0)
            if target is None:
                break
            scale = self.clutch.release_scale()
            tp, ty = target

            # Feedforward from the hand's own angular rate, plus proportional
            # correction. P alone cannot command anything until the camera has
            # already fallen behind the hand, which reads as rubber-band lag
            # no amount of gain fixes -- raising kp just makes it oscillate.
            vp, vy = self.clutch.feedforward(
                imu.pitch_rate if imu else 0.0,
                imu.yaw_rate if imu else 0.0)
            # deg/s throughout. The hand's own rate goes forward unchanged and
            # the P term closes the residual, both in the same units, so `kp`
            # means "deg/s per degree of error" instead of a deflection whose
            # effect varied across its own range.
            cap = response.MAX_DPS
            tilt = max(-cap, min(cap,
                       (vp + moves.wrap180(tp - att.pitch) * kp) * scale))
            pan = max(-cap, min(cap,
                      (vy + moves.wrap180(ty - att.yaw) * kp) * scale))
            # Kinetic control drives the stick directly rather than through
            # set_axes, so the limit warning has to be told about intent here
            # too or it goes blind the moment the clutch is engaged -- which is
            # exactly when the operator is moving fastest.
            self.limit_monitor.command(tilt / cap, pan / cap)
            stick.set_rate(tilt, pan)
        self.limit_monitor.command(0.0, 0.0)
        if self.stick is not None:
            self.stick.release()

    # -- arming ---------------------------------------------------------------

    def arm(self) -> None:
        self.require()
        self.armed = True
        self.disarm_reason = ""
        log.info("ARMED -- the rig may now move on its own")

    def stop_everything(self, reason: str = "STOP") -> None:
        """Universal stop. Immediate neutral, no easing, no confirmation."""
        self.clutch.abort()
        self._kinetic_stop.set()
        if self.runner is not None and self.runner.running:
            self.runner.stop(aborted=True)
        if self.stick is not None:
            # abort, not release: a stop is not the moment for a settle.
            self.stick.abort()
        self.limit_monitor.command(0.0, 0.0)
        self.owner = "none"
        self.disarm(reason)

    def disarm(self, reason: str = "operator") -> None:
        was = self.armed
        self.armed = False
        self.disarm_reason = reason
        # A timelapse is a programmed move stretched over hours. Every path
        # that disarms -- STOP, manual takeover, the Core2, /api/disarm --
        # must also stop it between frames, or the next hop fires a photo
        # from a rig nobody is watching.
        self._tl_stop.set()
        self._cancel_roll()
        if self.runner is not None and self.runner.running:
            self.runner.stop(aborted=True)
        if was:
            log.info("disarmed: %s", reason)

    def cue_go(self) -> None:
        if self.runner is None:
            raise RuntimeError("not connected")
        self.runner.go()

    # -- camera -------------------------------------------------------------

    def camera_set(self, what: str, value) -> None:
        link, _ = self.require()
        builders = {
            "iso": lambda v: camera.set_iso(str(v)),
            "shutter": lambda v: camera.set_shutter(int(v)),
            "manual": lambda v: camera.set_exposure_manual(_strict_bool(v, "manual")),
            "ev": lambda v: camera.set_ev(int(v)),
            "wb": lambda v: camera.set_white_balance(None if v in (None, "auto") else int(v)),
            "color": lambda v: camera.set_color_mode(str(v)),
            "zoom": lambda v: camera.set_zoom(float(v)),
            "focus_continuous": lambda v: camera.set_focus_mode(
                _strict_bool(v, "focus_continuous")),
        }
        if what not in builders:
            raise ValueError(f"unknown camera setting: {what}")
        link.send_frame(builders[what](value))
        # Remember what we ASKED for. The camera does not report its exposure
        # back over any command we have implemented, so this is the
        # controller's request, never the camera's state, and every consumer is
        # told so via `reported: False`. A monitor that presents a requested
        # value as a measured one is how a shot gets exposed wrong.
        self.camera_state[what] = value

    # -- shutter angle ---------------------------------------------------------

    def _assumed_fps(self) -> float:
        """The rate the shutter angle is measured against.

        Falls back to 24 rather than refusing: an angle against an assumed
        rate is still the right way to think about shutter, and the status
        marks it assumed so the number is never mistaken for a reading.
        """
        try:
            return float(self.camera_state.get("fps") or 24.0)
        except (TypeError, ValueError):
            return 24.0

    def shutter_info(self) -> dict:
        """The current shutter as an angle, with a flicker verdict."""
        denom = self.camera_state.get("shutter")
        fps = self._assumed_fps()
        out = {
            "fps": fps,
            "fps_reported": "fps" in self.camera_state,
            "mains_hz": self.mains_hz,
            "safe": shutter.flicker_safe(self.mains_hz, fastest=500),
        }
        if denom:
            out.update(shutter.describe(float(denom), fps, self.mains_hz))
        else:
            out["denominator"] = None
        return out

    def set_shutter_angle(self, angle: float) -> dict:
        """Pick the shutter speed that gives this angle at the current rate.

        The camera is told a speed, because that is the command it has. The
        operator is thinking in angles, because that is what determines the
        motion look and it has to follow the frame rate to stay constant.
        """
        fps = self._assumed_fps()
        denom = shutter.angle_to_standard(angle, fps)
        self.camera_set("shutter", denom)
        return self.shutter_info()

    def set_mains(self, hz: float) -> dict:
        """Which supply the lights are on. Changes the flicker verdict only --
        it sends nothing to the camera."""
        if hz not in (50.0, 60.0):
            raise ValueError("mains frequency is 50 or 60 Hz")
        self.mains_hz = float(hz)
        return self.shutter_info()

    def camera_resolution(self, resolution: str, fps: str) -> None:
        link, _ = self.require()
        link.send_frame(camera.set_resolution(resolution, fps))
        self.camera_state["resolution"] = resolution
        self.camera_state["fps"] = fps

    # -- take log -------------------------------------------------------------

    def log_take(self, note: str = "", circled: bool = False,
                 move_name: str = "") -> dict:
        """Record what was shot. A take log is the point of a slate."""
        # The validity record is what separates "the director liked it" from
        # "the rig actually repeated the move".
        report = self.runner.report.to_dict() if self.runner is not None else None
        entry = {
            # Stable within the session. Compare and export address a take by
            # this, never by its position in whatever slice a client saw.
            "id": len(self.takes),
            "scene": self.slate["scene"],
            "shot": self.slate["shot"],
            "take": self.slate["take"],
            "note": note,
            "circled": circled,
            "move": move_name or (self.move.name if self.move.waypoints else ""),
            "duration": round(self.move.total_duration, 2) if self.move.waypoints else 0.0,
            "setup": self.move.setup_summary(),
            "motion": report,
            "at": time.strftime("%H:%M:%S"),
        }
        self.takes.append(entry)
        self.slate["take"] += 1
        return entry

    def compare_takes(self, first: int, second: int,
                      fov_deg: float | None = None,
                      width_px: int = repeatability.DEFAULT_WIDTH_PX) -> dict:
        """Did those two takes actually match?

        Indices into the take log. The comparison is between the MEASURED
        traces: both takes were commanded the same path, so comparing the
        commanded path proves nothing.
        """
        for i in (first, second):
            if not (0 <= i < len(self.takes)):
                raise ValueError(f"no take at index {i}")
        if first == second:
            raise ValueError("a take always matches itself")

        traces = []
        for i in (first, second):
            motion = self.takes[i].get("motion") or {}
            trace = motion.get("trace") or []
            if len(trace) < 2:
                raise ValueError(
                    f"take {i + 1} has no motion trace -- it was logged "
                    "without a move having been played")
            traces.append(trace)

        # The operator's stated lens beats the house assumption, and the
        # result says which it used: every pixel figure here depends on the
        # field of view and nothing on this camera reports it.
        stated = self.move.fov_deg()
        used = fov_deg if fov_deg is not None else stated
        out = repeatability.compare(
            traces[0], traces[1],
            fov_deg=used if used is not None else repeatability.DEFAULT_FOV_DEG,
            width_px=width_px).to_dict()
        out["fov_assumed"] = used is None
        out["fov_source"] = ("request" if fov_deg is not None
                             else "shot setup" if stated is not None
                             else "house default")
        out["takes"] = [self._take_label(first), self._take_label(second)]
        return out

    def _take_label(self, i: int) -> str:
        t = self.takes[i]
        return f"{t.get('scene', '')}{t.get('shot', '')}/{t.get('take', i + 1)}"

    def set_slate(self, scene: str | None, shot: str | None, take: int | None) -> None:
        if scene is not None:
            self.slate["scene"] = str(scene)
        if shot is not None:
            self.slate["shot"] = str(shot)
        if take is not None:
            self.slate["take"] = max(1, int(take))

    def circle_last_take(self) -> None:
        if not self.takes:
            raise RuntimeError("no takes logged yet")
        self.takes[-1]["circled"] = not self.takes[-1]["circled"]

    def move_path(self, samples: int = 240) -> dict:
        """The move sampled by the real sampler, for drawing.

        The panel draws a map of where the move goes. It could sample the path
        itself in JavaScript, and the first version did -- but that is a second
        implementation of easing, dwell handling and arc routing, and the day
        it drifts from this one the map shows a move that will not be played.
        One sampler, asked over HTTP.
        """
        samples = max(2, min(2000, int(samples)))
        total = self.move.total_duration
        if len(self.move.waypoints) < 2 or total <= 0:
            return {"points": [], "duration": 0.0}
        points = []
        for i in range(samples + 1):
            got = self.move.sample(total * i / samples)
            if got is not None:
                points.append([round(got[0], 3), round(got[1], 3)])
        return {"points": points, "duration": total}

    # -- between takes -----------------------------------------------------------

    def retime_move(self, factor: float | None = None,
                    total: float | None = None) -> dict:
        self.move = self.move.retimed(factor=factor, total=total)
        return {"total_duration": round(self.move.total_duration, 2)}

    def offset_move(self, pitch: float = 0.0, yaw: float = 0.0) -> dict:
        self.move = self.move.offset(pitch=pitch, yaw=yaw)
        return {"waypoints": [w.to_dict() for w in self.move.waypoints]}

    def reference_move_here(self) -> dict:
        """Shift the stored move so it starts from where the head is now.

        The routine after a remount or a battery swap. Without it a library
        move is only usable from the exact rigging it was authored on, which
        on a real set it never is.
        """
        link, _ = self.require()
        att = getattr(link, "attitude", None)
        if att is None:
            raise RuntimeError("no telemetry yet -- cannot re-reference")
        self.move = self.move.referenced_to(att.pitch, att.yaw)
        return {"waypoints": [w.to_dict() for w in self.move.waypoints]}

    # -- path export ---------------------------------------------------------------

    def export_path(self, take: int | None = None, fmt: str = "csv",
                    fps: float = pathexport.DEFAULT_FPS,
                    vfov_deg: float | None = None) -> dict:
        """The measured camera path, for post.

        Measured, not commanded: those differ by the tracking error, and the
        tracking error is exactly what a compositor is trying to account for.
        """
        if take is None:
            if not self.takes:
                raise ValueError("no takes logged yet")
            take = len(self.takes) - 1
        if not (0 <= take < len(self.takes)):
            raise ValueError(f"no take at index {take}")
        motion = self.takes[take].get("motion") or {}
        trace = motion.get("trace") or []
        if len(trace) < 2:
            raise ValueError(
                f"take {take + 1} has no motion trace -- it was logged "
                "without a move having been played")

        if fmt not in ("csv", "chan"):
            raise ValueError(f"unknown export format {fmt!r}")
        writer = pathexport.to_csv if fmt == "csv" else pathexport.to_chan
        return {
            "format": fmt,
            "name": f"{self._take_label(take).replace('/', '-')}.{fmt}",
            "summary": pathexport.describe(trace, fps=fps),
            "body": writer(trace, fps=fps, vfov_deg=vfov_deg),
        }

    # -- frame census -----------------------------------------------------------

    def census_report(self, unknown_only: bool = True) -> dict:
        return self.census.report(unknown_only=unknown_only)

    def census_mark(self, label: str) -> dict:
        """Segment the capture. Comparing before and after a mark is what
        identifies a flag: press record, mark it, stop, mark it again."""
        self.census.mark(label)
        return {"marks": self.census.report()["marks"]}

    def census_reset(self) -> dict:
        self.census.reset()
        return self.census.report()

    # -- motion timelapse -----------------------------------------------------

    def timelapse_plan(self, **kw) -> dict:
        """Cost the shoot without starting it.

        Planning is separate from running because the numbers are the point:
        an operator needs to see that the shoot takes two hours and judders at
        thirty frames BEFORE committing an afternoon to it.
        """
        return timelapse.plan_for(self.move, **kw).to_dict()

    def timelapse_resume_info(self) -> dict:
        """Is there an interrupted shoot worth picking up?

        Reported rather than resumed automatically. Restarting into a shoot
        whose scene was struck two hours ago would be worse than losing it.
        """
        got = timelapse.load_progress(MOVES_DIR, self.move, now=time.time())
        if got is None:
            return {"available": False}
        return {
            "available": not got["finished"],
            "frame": got["frame"], "frames": got["frames"],
            "matches": got.get("matches", False),
            "stale": got["stale"], "finished": got["finished"],
            "plan": got.get("plan"),
        }

    def timelapse_start(self, resume: bool = False, **kw) -> dict:
        """Step the head through the move, taking one frame at each stop."""
        _, _ = self.require()
        if self.tl_state["running"]:
            raise RuntimeError("a timelapse is already running")
        if self.runner is None:
            raise RuntimeError("not connected")
        if not self.armed:
            raise RuntimeError("not armed -- arm before starting a timelapse")
        if self.clutch.state.engaged:
            raise RuntimeError("clutch is held -- release it first")
        if self.runner.running:
            raise RuntimeError("a move is running -- stop it first")

        done = 0
        if resume:
            got = timelapse.load_progress(MOVES_DIR, self.move, now=time.time())
            if got is None:
                raise RuntimeError("there is no interrupted timelapse to resume")
            if not got.get("matches"):
                raise RuntimeError(
                    "the move has been edited since that timelapse stopped -- "
                    "resuming would step the head somewhere the earlier frames "
                    "never went, and the join would not show until playback")
            if got["finished"]:
                raise RuntimeError("that timelapse already finished")
            done = int(got["frame"])
            # Reuse the ORIGINAL plan: re-costing from the current form fields
            # would change the frame spacing halfway through the sequence.
            plan = timelapse.Plan(**{
                k: got["plan"][k] for k in
                ("frames", "settle_s", "expose_s", "gap_s", "output_fps", "mode")
            }, pitch_span=0.0, yaw_span=0.0)
        else:
            plan = timelapse.plan_for(self.move, **kw)

        poses = timelapse.frame_poses(self.move, plan.frames)
        if len(poses) < 2:
            raise RuntimeError("the move has nothing to step through")

        if resume:
            poses = timelapse.remaining_poses(self.move, plan, done)
            if not poses:
                raise RuntimeError("nothing left to shoot")
            # After a power cycle the head has re-homed and the stored angles
            # may no longer mean what they meant. Demanding the camera be back
            # on the next frame IS the re-reference discipline, enforced rather
            # than trusted.
            att = getattr(self.link, "attitude", None)
            if att is None:
                raise RuntimeError("no telemetry -- cannot confirm where the head is")
            want_p, want_y = poses[0]
            dp = moves.wrap180(att.pitch - want_p)
            dy = moves.wrap180(att.yaw - want_y)
            if max(abs(dp), abs(dy)) > moves.START_TOLERANCE_DEG:
                raise RuntimeError(
                    f"the head is {dp:+.1f} deg tilt and {dy:+.1f} deg pan from "
                    f"frame {done + 1}. Put the camera back on it before "
                    f"resuming, or the sequence will jump.")

        self.owner = "program"
        self._tl_stop.clear()
        self.tl_state = {"running": True, "frame": done, "frames": plan.frames,
                         "error": None, "plan": plan.to_dict(),
                         "resumed_from": done if resume else 0}
        self._tl_thread = threading.Thread(
            target=self._timelapse_worker, args=(poses, plan, done), daemon=True)
        self._tl_thread.start()
        return dict(self.tl_state)

    def timelapse_stop(self) -> None:
        """Stop between frames. The head is left wherever it got to."""
        self._tl_stop.set()

    def _timelapse_worker(self, poses, plan, done: int = 0) -> None:
        # A hop per frame, reusing the same runner that plays an ordinary move
        # rather than a second motion path -- one place where easing, soft
        # limits and the deadman live.
        hop_s = max(0.25, plan.settle_s)
        try:
            for i, (pitch, yaw) in enumerate(poses):
                if self._tl_stop.is_set():
                    break
                if not self.armed:
                    raise RuntimeError("disarmed mid-timelapse")

                hop = moves.Move(name="tl", waypoints=[
                    moves.Waypoint("from", pitch=self._now_pitch(pitch),
                                   yaw=self._now_yaw(yaw)),
                    moves.Waypoint("to", pitch=pitch, yaw=yaw,
                                   duration=hop_s,
                                   easing=moves.DEFAULT_EASING),
                ])
                self.runner.start(hop)
                if not self._wait_for_runner(hop_s + 5.0):
                    raise RuntimeError(f"frame {i + 1}: the head did not arrive")

                # Settle AFTER arriving. The head rings for a moment at the end
                # of every hop, and a frame taken during that is soft in a way
                # that is invisible on a monitor and obvious in the sequence.
                if self._sleep_or_stop(plan.settle_s):
                    break

                link = self.link
                if link is not None:
                    link.send_frame(commands.photo())
                shot = done + i + 1
                self.tl_state = dict(self.tl_state, frame=shot)
                # Written after every frame. A shoot that dies at frame 812 of
                # 1200 is recoverable; one that dies without a record is a lost
                # afternoon.
                try:
                    timelapse.save_progress(MOVES_DIR, self.move, plan, shot,
                                            time.time())
                except OSError:
                    log.warning("could not save timelapse progress", exc_info=True)

                if self._sleep_or_stop(plan.expose_s + plan.gap_s):
                    break
        except Exception as exc:                    # noqa: BLE001 - reported
            log.exception("timelapse failed")
            self.tl_state = dict(self.tl_state, error=f"{type(exc).__name__}: {exc}")
        finally:
            stick = self.stick
            if stick is not None:
                stick.abort()
            # A finished shoot offering to resume itself is a trap the morning
            # after; an interrupted one must keep its place.
            if self.tl_state.get("frame", 0) >= self.tl_state.get("frames", 0):
                timelapse.clear_progress(MOVES_DIR)
            self.tl_state = dict(self.tl_state, running=False)
            if self.owner == "program":
                self.owner = "none"

    def _now_pitch(self, fallback: float) -> float:
        att = getattr(self.link, "attitude", None)
        return fallback if att is None else att.pitch

    def _now_yaw(self, fallback: float) -> float:
        att = getattr(self.link, "attitude", None)
        return fallback if att is None else att.yaw

    def _wait_for_runner(self, timeout: float) -> bool:
        """True once the hop has finished. False if it never does."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._tl_stop.is_set():
                return True
            if self.runner is not None and not self.runner.running:
                return True
            time.sleep(0.02)
        return False

    def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep, but wake immediately on stop. True if we were stopped.

        A plain sleep here would make the stop button take a whole frame
        interval to answer, which on a slow timelapse is minutes.
        """
        return self._tl_stop.wait(max(0.0, seconds))

    # -- pre-flight ---------------------------------------------------------------

    def preflight(self, at_preset: bool = True) -> dict:
        """Report what the rig cannot do with the move currently loaded.

        Reports rather than blocks. `MoveRunner` already clamps pitch to the
        soft limit while running, so a move that leaves the arc is not a crash
        -- it is a move that quietly arrives somewhere other than where it was
        authored. This says where and by how much, before the take rather than
        after it.

        Checked against the selected sensitivity by default, because "the head
        could do this at full throw" is not the useful question when the
        operator is shooting on `fine`.
        """
        cap = (response.SPEED_CAPS.get(self.speed_preset,
                                       response.SPEED_CAPS["normal"])
               if at_preset else response.MAX_DPS)
        report = preflight.check(self.move, max_dps=cap).to_dict()
        report["speed_preset"] = self.speed_preset if at_preset else "full throw"
        report["max_dps"] = cap
        return report

    # -- shot library -----------------------------------------------------------

    def save_move(self, name: str) -> dict:
        """Put the move being edited into the library under `name`."""
        entry = self.library.save(name, self.move)
        self.move.name = entry.name
        return entry.to_dict()

    def load_move(self, name: str) -> dict:
        """Recall a saved move for editing and playback.

        Refused while a move is running. Swapping the path underneath a runner
        mid-take would have the rig finish a move it never started, which is
        the sort of surprise a motion-control rig exists to eliminate.
        """
        if self.runner is not None and self.runner.running:
            raise RuntimeError("a move is running -- stop it before loading another")
        self.move = self.library.load(name)
        return {"name": self.move.name,
                "waypoints": [w.to_dict() for w in self.move.waypoints],
                "loop": self.move.loop, "ping_pong": self.move.ping_pong,
                "setup": dict(self.move.setup)}

    def delete_move(self, name: str) -> bool:
        return self.library.delete(name)

    def list_moves(self) -> list[dict]:
        return [e.to_dict() for e in self.library.list()]

    # -- monitor LUT ----------------------------------------------------------

    def load_lut(self, text: str, name: str = "") -> dict:
        """Parse a .cube and hold it for the viewer.

        Parsed here rather than in the browser because a malformed .cube does
        not fail loudly -- it produces a table that is subtly wrong, and the
        operator grades against it. Rejecting it with a message that names the
        line is the whole point of doing this server side.

        This is a MONITOR look. It changes what the operator sees and nothing
        the camera records, which is exactly the distinction a viewing LUT is
        for -- and the viewer has to keep saying so, or someone will think the
        grade is baked in.
        """
        table = lut.parse_cube(text)
        self.lut = table
        self.lut_name = name or table.title or "look"
        return self.lut_info()

    def clear_lut(self) -> dict:
        self.lut = None
        self.lut_name = ""
        return self.lut_info()

    def lut_info(self) -> dict:
        """What the viewer needs to label the look, without the whole table."""
        if self.lut is None:
            return {"loaded": False, "name": "", "size": 0, "dimensions": 0}
        return {"loaded": True, "name": self.lut_name,
                "size": self.lut.size, "dimensions": self.lut.dimensions,
                "title": self.lut.title}

    # -- moves --------------------------------------------------------------

    def capture_waypoint(self, name: str = "", flow: bool = False,
                         zoom: float | None = None,
                         zoom_easing: str = moves.DEFAULT_EASING,
                         easing: str = moves.DEFAULT_EASING,
                         duration: float = 3.0) -> dict:
        """Author a node by demonstration: drive there, then capture it.

        The angles are measured -- they come off the telemetry the gimbal is
        already reporting. The zoom is not: nothing on this camera reports its
        zoom back over any command implemented here, so the best available
        value is the last one this controller asked for. If it has never asked,
        the node leaves zoom unset rather than guessing a number, because an
        invented 0.0 would rack the lens wide between two framings that were
        captured to match.
        """
        link, _ = self.require()
        att = link.attitude
        if att is None:
            raise RuntimeError("no telemetry yet -- cannot capture a position")
        if zoom is None:
            asked = self.camera_state.get("zoom")
            zoom = None if asked is None else float(asked)
        wp = moves.Waypoint(
            name=name or chr(ord("A") + len(self.move.waypoints) % 26),
            pitch=round(att.pitch, 1), yaw=round(att.yaw, 1),
            duration=max(0.1, float(duration)), dwell=0.0, easing=easing,
            zoom=zoom, zoom_easing=zoom_easing, flow=bool(flow))
        self.move.waypoints.append(wp)
        return wp.to_dict()

    def set_move(self, data: dict) -> None:
        self.move = moves.Move.from_dict(data)

    def set_setup(self, fields: dict) -> None:
        """Record how the camera is rigged, alongside the move."""
        self.move.setup.update({k: v for k, v in fields.items()
                                if k in moves.Move.SETUP_FIELDS})

    def _play_preflight(self, force: bool = False) -> None:
        """Everything play() checks before it commits, so ROLL can check the
        same things before it commits the camera as well."""
        link, _ = self.require()
        if self.clutch.state.engaged:
            raise RuntimeError("clutch is held -- release it before playing a move")
        if self.runner is None:
            raise RuntimeError("not connected")
        if not self.armed:
            raise RuntimeError("not armed -- arm before playing a move")

        # Somebody will knock the tripod between takes and nobody will admit
        # it. The move then runs perfectly from the wrong place, which looks
        # like a working rig producing an unusable take -- and it is only
        # discovered in review. Checked here rather than left to the operator
        # to remember.
        #
        # Skipped when there is no telemetry: the runner needs attitude anyway
        # and will fail with a clearer message than this one could.
        att = getattr(link, "attitude", None)
        if att is not None and not force and self.move.waypoints:
            if not self.move.at_start(att.pitch, att.yaw):
                dp, dy = self.move.start_error(att.pitch, att.yaw)
                raise RuntimeError(
                    f"the head is {dp:+.1f} deg tilt and {dy:+.1f} deg pan from "
                    f"the start of this move. Back to one first, or play with "
                    f"force to run it from here.")

    def play(self, force: bool = False) -> None:
        self._play_preflight(force)
        self.owner = "program"
        self.runner.start(self.move)

    # -- pre-roll -------------------------------------------------------------
    # "Roll camera" and "action" are two calls on a set, seconds apart, and
    # the gap is the editorial handle. ROLL is one call here: the same
    # preflight as play, then record, then the countdown cue, then the move.
    # Nothing moves until the timer fires, and any disarm cancels it.
    def roll(self, preroll_s: float | None = None, force: bool = False) -> dict:
        self._play_preflight(force)
        seconds = float(self.preroll_s if preroll_s is None else preroll_s)
        if not (0.0 <= seconds <= 30.0):
            raise ValueError("preroll must be 0..30 seconds")
        self._cancel_roll()
        self.action("record_start")
        self.cue("count")
        self.preroll_until = time.time() + seconds
        timer = threading.Timer(seconds, self._roll_go)
        timer.daemon = True
        self._roll_timer = timer
        timer.start()
        return {"preroll": seconds, "at": self.preroll_until}

    def _roll_go(self) -> None:
        self._roll_timer = None
        self.preroll_until = 0.0
        # The world may have changed during the count: a STOP, a grab, a
        # dropped link. Re-check rather than trust the timer.
        if not self.armed or self.runner is None or self.clutch.state.engaged:
            return
        self.owner = "program"
        self.runner.start(self.move)

    def _cancel_roll(self) -> None:
        timer = getattr(self, "_roll_timer", None)
        if timer is not None:
            timer.cancel()
        self._roll_timer = None
        self.preroll_until = 0.0

    def play_segment(self, first: int, last: int, force: bool = False) -> dict:
        """Rehearse part of the move.

        "Again from node 3" is how blocking works: the difficult third of a
        shot gets run twenty times and the easy first third once. Replaying
        the whole move to reach the part being worked on wastes the crew's
        time and the battery.

        The full move is left untouched -- this plays a copy, so the next
        Play is still the shot.
        """
        _, _ = self.require()
        if self.runner is None:
            raise RuntimeError("not connected")
        if not self.armed:
            raise RuntimeError("not armed -- arm before rehearsing")
        if self.clutch.state.engaged:
            raise RuntimeError("clutch is held -- release it first")
        seg = self.move.segment(first, last)

        link = self.link
        att = getattr(link, "attitude", None) if link else None
        if att is not None and not force and not seg.at_start(att.pitch, att.yaw):
            dp, dy = seg.start_error(att.pitch, att.yaw)
            raise RuntimeError(
                f"the head is {dp:+.1f} deg tilt and {dy:+.1f} deg pan from "
                f"node {min(first, last) + 1}. Go there first, or force it.")

        self.owner = "program"
        self.runner.start(seg)
        return {"name": seg.name, "duration": round(seg.total_duration, 2)}

    def back_to_one(self, duration: float = 2.5) -> None:
        """Return to the top of the move. One button, because it is the most
        frequent thing anyone does between takes."""
        if not self.move.waypoints:
            raise RuntimeError("no move to return to")
        self.goto(0, duration=duration)

    def start_check(self) -> dict:
        """Where the head is relative to the top of the move."""
        link = self.link
        att = getattr(link, "attitude", None) if link else None
        if att is None or not self.move.waypoints:
            return {"known": False, "at_start": False,
                    "tilt_error": None, "pan_error": None}
        dp, dy = self.move.start_error(att.pitch, att.yaw)
        return {"known": True,
                "at_start": self.move.at_start(att.pitch, att.yaw),
                "tilt_error": round(dp, 2), "pan_error": round(dy, 2),
                "tolerance": moves.START_TOLERANCE_DEG}

    def stop_move(self) -> None:
        if self.runner is not None:
            self.runner.stop(aborted=True)

    def goto(self, index: int, duration: float = 2.5) -> None:
        """Travel to one waypoint from wherever the camera is now."""
        link, _ = self.require()
        if self.runner is None:
            raise RuntimeError("not connected")
        if not (0 <= index < len(self.move.waypoints)):
            raise ValueError(f"no waypoint at index {index}")
        att = link.attitude
        if att is None:
            raise RuntimeError("no telemetry yet")
        target = self.move.waypoints[index]
        hop = moves.Move(name="goto", waypoints=[
            moves.Waypoint("here", pitch=att.pitch, yaw=att.yaw),
            moves.Waypoint(target.name, pitch=target.pitch, yaw=target.yaw,
                           duration=max(0.2, duration),
                           easing=moves.DEFAULT_EASING),
        ])
        self.runner.start(hop)

    def action(self, name: str) -> None:
        link, stick = self.require()
        if name == "recenter":
            stick.recenter()
        elif name == "flip":
            stick.flip()
        elif name == "follow":
            stick.follow_mode()
        elif name == "fpv":
            stick.fpv_mode()
        elif name == "record_start":
            link.send_frame(commands.record(True))
            self._set_recording(True)
        elif name == "record_stop":
            link.send_frame(commands.record(False))
            self._set_recording(False)
        elif name == "photo":
            link.send_frame(commands.photo())
        elif name == "live_view":
            link.send_frame(commands.live_view_enable())
        elif name == "params":
            link.send_frame(commands.gimbal_params_get())
        else:
            raise ValueError(f"unknown action: {name}")


class Handler(BaseHTTPRequestHandler):
    session: CameraSession = None  # set on the class before serving
    token: str | None = None       # None means no auth (loopback only)

    @property
    def route(self) -> str:
        """Path without the query string. Every comparison must use this."""
        return self.path.split("?", 1)[0]

    def _is_loopback(self) -> bool:
        return str(self.client_address[0]) in ("127.0.0.1", "::1")

    def _authorised(self) -> bool:
        """Token in the query string, a cookie, or a header.

        This surface physically moves a camera, so once it is reachable from
        anything but loopback it needs a gate. The token is deliberately
        cheap -- it stops the other people on a set's Wi-Fi from panning your
        camera; it is not protection against someone who wants in.
        """
        if self.token is None:
            return True
        # Read-only diagnostics from this machine need no token. The token
        # exists to stop other people on a set's Wi-Fi driving the camera; it
        # should not stop the operator reading health from their own console.
        if self.route == "/api/diag" and self._is_loopback():
            return True
        want = self.token
        query_tokens = parse_qs(urlsplit(self.path).query,
                                keep_blank_values=True).get("t", ())
        if any(secrets.compare_digest(token, want) for token in query_tokens):
            return True
        if secrets.compare_digest(self.headers.get("X-Osmo-Token", ""), want):
            return True
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie") or "")
        except (CookieError, ValueError):
            return False
        morsel = cookies.get("osmo_token")
        return morsel is not None and secrets.compare_digest(morsel.value, want)

    def log_message(self, fmt, *a):  # quieter than the default access log
        log.debug(fmt, *a)

    # -- helpers ------------------------------------------------------------

    def _deny(self) -> None:
        body = (b"<h3>Osmo panel</h3><p>Add the access token to the URL: "
                b"<code>?t=YOUR_TOKEN</code></p>")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        if self.token:
            # Set once from ?t=, so in-page fetches need no query string.
            self.send_header("Set-Cookie",
                             f"osmo_token={self.token}; Path=/; SameSite=Strict")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _mjpeg(self) -> None:
        """multipart/x-mixed-replace: the browser renders it in a plain <img>."""
        live = self.session.live
        if live is None:
            self.send_error(503, "live view not running")
            return
        boundary = "osmoframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = -1
        try:
            while True:
                jpg, seen = live.wait_for_frame(since=seen, timeout=5.0)
                if jpg is None:
                    if self.session.live is None:
                        return
                    continue
                self.wfile.write(f"--{boundary}\r\n".encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # viewer navigated away

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > MAX_JSON_BODY_BYTES:
            raise ValueError(f"JSON request body exceeds {MAX_JSON_BODY_BYTES} bytes")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("request body must be valid JSON") from None
        if not isinstance(body, dict):
            raise ValueError("JSON request body must be an object")
        return body

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        if not self._authorised():
            self._deny()
            return
        # The monitor is the landing page. It is what the camera operator uses;
        # the engineering panel is for building moves and diagnosing the rig,
        # which is a different job done at a different time. Having the
        # engineering view on "/" meant the monitor was effectively invisible.
        if self.route in ("/", "/cine", "/cine.html"):
            self._file(WEB_DIR / "cine.html", "text/html; charset=utf-8")
        elif self.route in ("/panel", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif self.route in ("/mobile", "/mobile.html"):
            self._file(WEB_DIR / "mobile.html", "text/html; charset=utf-8")
        elif self.route == "/monitor.js":
            # Assists and scopes, shared by both pages so the exposure maths
            # cannot drift into two disagreeing copies.
            self._file(WEB_DIR / "monitor.js", "application/javascript; charset=utf-8")
        elif self.route == "/api/status":
            self._json(self.session.status())
        elif self.route == "/api/takes":
            # The whole log. Status carries only the tail, and a compare or an
            # export built from a tail silently addresses the wrong takes.
            self._json({"ok": True, "takes": self.session.takes})
        elif self.route == "/api/move/path":
            self._json(self.session.move_path())
        elif self.route == "/api/timelapse/resume":
            self._json(self.session.timelapse_resume_info())
        elif self.route == "/api/census":
            # Unknown opcodes by default: what we already decode is not the
            # interesting part.
            self._json(self.session.census_report(
                unknown_only="all" not in (self.path.split("?", 1)[-1] or "")))
        elif self.route == "/api/preflight":
            self._json(self.session.preflight())
        elif self.route == "/api/moves":
            self._json({"moves": self.session.list_moves()})
        elif self.route == "/api/lut":
            # The table itself, only when asked for: a 33-cube is 107k floats
            # and has no business riding along with every status poll.
            table = self.session.lut
            self._json({"lut": None if table is None else table.to_dict(),
                        "info": self.session.lut_info()})
        elif self.route == "/api/stream":
            self._mjpeg()
        elif self.route == "/api/snapshot":
            live = self.session.live
            jpg = live.snapshot() if live else None
            if not jpg:
                self.send_error(503, "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpg)
        elif self.route == "/api/diag":
            live = self.session.live
            self._json({
                "state": self.session.state,
                "live": live.stats() if live else {"running": False},
                "video_packets": (self.session.link.video_packets
                                  if self.session.link else 0),
                "core2": (self.session.core2.status.to_dict()
                          if self.session.core2 else {"connected": False}),
                "owner": self.session.owner,
                "armed": self.session.armed,
            })
        elif self.route == "/api/presets":
            self._json({"presets": moves.PRESETS,
                        "easings": sorted(moves.EASINGS),
                        "iso": list(camera.ISO_INDEX),
                        "shutter": camera.SHUTTER_DENOMS,
                        "color": list(camera.COLOR_MODES),
                        "resolutions": list(camera.RESOLUTIONS),
                        "fps": list(camera.FPS_INDEX),
                        "gains": sorted(GAINS, key=lambda k: GAINS[k])})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._authorised():
            self._deny()
            return
        try:
            if self.route == "/api/connect":
                self.session.connect()
                self._json({"ok": True})
            elif self.route == "/api/disconnect":
                self.session.disconnect()
                self._json({"ok": True})
            elif self.route == "/api/stick":
                body = self._body()
                self.session.set_axes(float(body.get("tilt", 0.0)),
                                      float(body.get("pan", 0.0)))
                self._json({"ok": True})
            elif self.route == "/api/camera":
                b = self._body()
                self.session.camera_set(str(b.get("set", "")), b.get("value"))
                self._json({"ok": True})
            elif self.route == "/api/camera/resolution":
                b = self._body()
                self.session.camera_resolution(str(b.get("resolution", "4K")),
                                               str(b.get("fps", "25")))
                self._json({"ok": True})
            elif self.route == "/api/take":
                b = self._body()
                self._json({"ok": True, "take": self.session.log_take(
                    str(b.get("note", "")),
                    _strict_bool(b.get("circled", False), "circled"))})
            elif self.route == "/api/slate":
                b = self._body()
                self.session.set_slate(b.get("scene"), b.get("shot"), b.get("take"))
                self._json({"ok": True})
            elif self.route == "/api/take/circle":
                self.session.circle_last_take()
                self._json({"ok": True})
            elif self.route == "/api/waypoint":
                body = self._body()
                zoom = body.get("zoom")
                self._json({"ok": True,
                            "waypoint": self.session.capture_waypoint(
                                name=str(body.get("name", "")),
                                flow=_strict_bool(body.get("flow", False),
                                                  "flow"),
                                zoom=None if zoom is None else float(zoom),
                                zoom_easing=str(body.get("zoom_easing",
                                                         moves.DEFAULT_EASING)),
                                easing=str(body.get("easing",
                                                    moves.DEFAULT_EASING)),
                                duration=float(body.get("duration", 3.0)))})
            elif self.route == "/api/move":
                self.session.set_move(self._body())
                self._json({"ok": True})
            elif self.route == "/api/moves/save":
                self._json({"ok": True,
                            "entry": self.session.save_move(
                                str(self._body().get("name", "")))})
            elif self.route == "/api/moves/load":
                self._json({"ok": True,
                            "move": self.session.load_move(
                                str(self._body().get("name", "")))})
            elif self.route == "/api/moves/delete":
                self._json({"ok": True,
                            "deleted": self.session.delete_move(
                                str(self._body().get("name", "")))})
            elif self.route == "/api/census/mark":
                self._json({"ok": True, **self.session.census_mark(
                    str(self._body().get("label", "mark")))})
            elif self.route == "/api/census/reset":
                self._json({"ok": True, **self.session.census_reset()})
            elif self.route == "/api/takes/compare":
                body = self._body()
                self._json({"ok": True,
                            "comparison": self.session.compare_takes(
                                int(body.get("first", 0)),
                                int(body.get("second", 1)),
                                float(body.get("fov_deg",
                                               repeatability.DEFAULT_FOV_DEG)),
                                int(body.get("width_px",
                                             repeatability.DEFAULT_WIDTH_PX)))})
            elif self.route == "/api/timelapse/plan":
                self._json({"ok": True,
                            "plan": self.session.timelapse_plan(
                                **_timelapse_args(self._body()))})
            elif self.route == "/api/timelapse/start":
                body = self._body()
                self._json({"ok": True,
                            "timelapse": self.session.timelapse_start(
                                resume=_strict_bool(body.get("resume", False),
                                                    "resume"),
                                **_timelapse_args(body))})
            elif self.route == "/api/timelapse/stop":
                self.session.timelapse_stop()
                self._json({"ok": True})
            elif self.route == "/api/shutter":
                body = self._body()
                if "mains" in body:
                    self._json({"ok": True,
                                "shutter": self.session.set_mains(
                                    float(body["mains"]))})
                else:
                    self._json({"ok": True,
                                "shutter": self.session.set_shutter_angle(
                                    float(body.get("angle", shutter.CINE_ANGLE)))})
            elif self.route == "/api/lut":
                body = self._body()
                clear = _strict_bool(body.get("clear", False), "clear")
                if clear:
                    self._json({"ok": True, "info": self.session.clear_lut()})
                else:
                    self._json({"ok": True,
                                "info": self.session.load_lut(
                                    str(body.get("cube", "")),
                                    str(body.get("name", "")))})
            elif self.route == "/api/grab":
                self.session.grab("phone")
                self._json({"ok": True})
            elif self.route == "/api/release":
                self.session.let_go("phone")
                self._json({"ok": True})
            elif self.route == "/api/stop":
                self.session.stop_everything(str(self._body().get("reason", "STOP")))
                self._json({"ok": True})
            elif self.route == "/api/clutch/lock":
                b = self._body()
                tilt = (None if "tilt" not in b else
                        _strict_bool(b["tilt"], "tilt"))
                pan = (None if "pan" not in b else
                       _strict_bool(b["pan"], "pan"))
                self.session.clutch.set_locks(tilt=tilt, pan=pan)
                self._json({"ok": True})
            elif self.route == "/api/ramp":
                self.session.set_ramp(str(self._body().get("ramp", "fluid")))
                self._json({"ok": True})
            elif self.route == "/api/speed":
                self.session.set_speed(str(self._body().get("preset", "normal")))
                self._json({"ok": True})
            elif self.route == "/api/clutch/gain":
                self.session.clutch.set_gain(str(self._body().get("gain", "normal")))
                self._json({"ok": True})
            elif self.route == "/api/arm":
                self.session.arm()
                self._json({"ok": True})
            elif self.route == "/api/disarm":
                self.session.disarm(str(self._body().get("reason", "operator")))
                self._json({"ok": True})
            elif self.route == "/api/cue/go":
                self.session.cue_go()
                self._json({"ok": True})
            elif self.route == "/api/setup":
                self.session.set_setup(self._body())
                self._json({"ok": True})
            elif self.route == "/api/move/segment":
                body = self._body()
                self._json({"ok": True, **self.session.play_segment(
                    int(body.get("first", 0)), int(body.get("last", 1)),
                    force=_strict_bool(body.get("force", False), "force"))})
            elif self.route == "/api/move/retime":
                body = self._body()
                self._json({"ok": True, **self.session.retime_move(
                    factor=(float(body["factor"]) if "factor" in body else None),
                    total=(float(body["total"]) if "total" in body else None))})
            elif self.route == "/api/move/offset":
                body = self._body()
                self._json({"ok": True, **self.session.offset_move(
                    pitch=float(body.get("pitch", 0.0)),
                    yaw=float(body.get("yaw", 0.0)))})
            elif self.route == "/api/move/reference":
                self._json({"ok": True, **self.session.reference_move_here()})
            elif self.route == "/api/cue":
                self.session.cue(str(self._body().get("name", "")))
                self._json({"ok": True})
            elif self.route == "/api/backtoone":
                self.session.back_to_one(float(self._body().get("duration", 2.5)))
                self._json({"ok": True})
            elif self.route == "/api/move/export":
                body = self._body()
                self._json({"ok": True, **self.session.export_path(
                    take=(int(body["take"]) if "take" in body else None),
                    fmt=str(body.get("format", "csv")),
                    fps=float(body.get("fps", pathexport.DEFAULT_FPS)),
                    vfov_deg=(float(body["vfov_deg"]) if "vfov_deg" in body
                              else None))})
            elif self.route == "/api/move/play":
                body = self._body()
                self.session.play(
                    force=_strict_bool(body.get("force", False), "force"))
                self._json({"ok": True})
            elif self.route == "/api/move/roll":
                body = self._body()
                self._json({"ok": True, **self.session.roll(
                    preroll_s=(float(body["preroll"]) if "preroll" in body
                               else None),
                    force=_strict_bool(body.get("force", False), "force"))})
            elif self.route == "/api/move/stop":
                self.session.stop_move()
                self._json({"ok": True})
            elif self.route == "/api/goto":
                body = self._body()
                self.session.goto(int(body.get("index", 0)),
                                  float(body.get("duration", 2.5)))
                self._json({"ok": True})
            elif self.route == "/api/action":
                self.session.action(str(self._body().get("name", "")))
                self._json({"ok": True})
            else:
                self.send_error(404)
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, code=400)


def _local_ip_towards(host: str, port: int = 80) -> str | None:
    """Which local address the OS would use to reach `host`.

    A UDP connect sends nothing; it just makes the kernel pick a route and bind
    a source address. This beats enumerating adapters, which on Windows also
    turns up WSL and Hyper-V addresses that no phone can reach.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        ip = sock.getsockname()[0]
        return None if ip.startswith(("127.", "0.")) else ip
    except OSError:
        return None
    finally:
        sock.close()


def _timelapse_args(body: dict) -> dict:
    """Only the fields a timelapse actually takes, coerced.

    Not **body: the body is operator input and plan_for takes keyword
    arguments, so forwarding it wholesale lets a caller name any parameter it
    happens to have.
    """
    out: dict = {}
    if "frames" in body:
        out["frames"] = int(body["frames"])
    elif "playback_s" in body:
        out["frames"] = timelapse.frames_for_playback(
            float(body["playback_s"]),
            float(body.get("output_fps", timelapse.DEFAULT_OUTPUT_FPS)))
    for name in ("expose_s", "settle_s", "gap_s", "output_fps"):
        if name in body:
            out[name] = float(body[name])
    if "mode" in body:
        out["mode"] = str(body["mode"])
    return out


def _reachable_addresses(camera_host: str) -> list[tuple[str, str]]:
    """Addresses a phone could actually use, best first.

    The ordinary LAN comes first: the phone keeps its internet and avoids the
    client-isolation that many access points enforce. The camera's own AP is
    listed as a fallback and only works if it permits client-to-client
    traffic -- plenty of APs do not.
    """
    out: list[tuple[str, str]] = []
    lan = _local_ip_towards("8.8.8.8")
    if lan:
        out.append(("on your network", lan))
    cam = _local_ip_towards(camera_host, 9004)
    if cam and cam != lan:
        out.append(("via camera Wi-Fi", cam))
    return out


def build_parser() -> argparse.ArgumentParser:
    """The real CLI. Exposed so tests can assert it supplies every attribute
    CameraSession reads -- a hand-written fixture once invented `imu_port`,
    which passed 343 tests and then crashed the actual server on connect."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--lan", action="store_true",
                    help="serve on all interfaces so a phone or tablet can drive it")
    ap.add_argument("--bind", help="explicit bind address (implies --lan behaviour)")
    ap.add_argument("--token", help="access token; auto-generated when serving on a LAN")
    ap.add_argument("--no-token", action="store_true",
                    help="serve on the LAN with NO access control")
    ap.add_argument("--host", default=transport.CAMERA_HOST, help="camera address")
    ap.add_argument("--wifi-interface")
    ap.add_argument("--skip-ble", action="store_true")
    ap.add_argument("--skip-wifi-join", action="store_true")
    ap.add_argument("--ssid")
    ap.add_argument("--password")
    ap.add_argument("--pin", default=commands.DEFAULT_PIN)
    ap.add_argument("--name")
    ap.add_argument("--scan-timeout", type=float, default=15.0)
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--kp", type=float, default=2.0,
                    help="closed-loop gain, deg/s of correction per degree of error")
    ap.add_argument("--imu-port",
                    help="Core2 serial port, e.g. COM3 (autodetected otherwise)")
    ap.add_argument("--no-core2", action="store_true",
                    help="do not look for an M5Stack Core2 control surface")
    ap.add_argument("--no-live-view", action="store_true",
                    help="skip the HEVC decoder (control only)")
    ap.add_argument("--autoconnect", action="store_true",
                    help="connect to the camera as soon as the server starts")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    env = config.load()
    args.ssid = args.ssid or config.resolve(env, "ssid")
    args.password = args.password or config.resolve(env, "password")
    args.wifi_interface = args.wifi_interface or config.resolve(env, "wifi_interface")

    Handler.session = CameraSession(args)
    Handler.session.attach_core2()
    if args.autoconnect:
        Handler.session.connect()

    bind = args.bind or ("0.0.0.0" if args.lan else "127.0.0.1")
    exposed = bind not in ("127.0.0.1", "localhost")
    if exposed and not args.no_token:
        # Secure by default the moment it leaves loopback.
        Handler.token = args.token or secrets.token_urlsafe(9)
    elif args.token:
        Handler.token = args.token

    # Windows honours SO_REUSEADDR loosely: with the default
    # allow_reuse_address a SECOND server binds the same port and both run.
    # Two instances then both join the camera AP and both open a UDP datalink,
    # which the camera answers erratically -- it looks exactly like an
    # unstable link. Fail loudly instead.
    class SingleInstanceServer(ThreadingHTTPServer):
        allow_reuse_address = False

    try:
        server = SingleInstanceServer((bind, args.port), Handler)
    except OSError as exc:
        log.error("port %d is already in use (%s)", args.port, exc)
        log.error("another panel is running -- stop it, or pass --port")
        return 1
    suffix = f"?t={Handler.token}" if Handler.token else ""
    log.info("control panel on http://127.0.0.1:%d%s", args.port, suffix)
    if exposed:
        for label, ip in _reachable_addresses(args.host):
            log.info("  %-22s http://%s:%d%s", label, ip, args.port, suffix)
        if Handler.token:
            log.info("token: %s  (open the URL above on the phone)", Handler.token)
        else:
            log.warning("NO ACCESS CONTROL -- anyone on this network can move the camera")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        Handler.session.disconnect()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
