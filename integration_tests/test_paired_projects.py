"""Run explicitly with OSMOPALM_ROOT; never opens hardware or a network."""
import json
import os
from pathlib import Path
import re
import unittest
from driver import ble, commands, core2, datalink, transport

ROOT = Path(__file__).resolve().parents[1]


class TestPairedProjects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get("OSMOPALM_ROOT")
        if not value:
            raise RuntimeError("Set OSMOPALM_ROOT to the OsmoPalm checkout; this gate must not silently skip.")
        cls.palm = Path(value).resolve()
        cls.main = (cls.palm / "firmware/core2_panel/src/main.cpp").read_text(encoding="utf-8")
        cls.direct = (cls.palm / "firmware/core2_panel/src/direct_camera.cpp").read_text(encoding="utf-8")
        cls.server = (ROOT / "server.py").read_text(encoding="utf-8")

    def test_contract_snapshots_are_identical(self):
        self.assertEqual((ROOT / "contracts/camera-control-v1.json").read_bytes(),
                         (self.palm / "contracts/camera-control-v1.json").read_bytes())

    def test_actual_ble_constants_and_endpoints_agree(self):
        constants = dict(re.findall(r'constexpr char (\w+)\[\] = "([^"]*)";', self.direct))
        for key, expected in (("SERVICE_UUID", ble.SERVICE_UUID), ("NOTIFY_UUID", ble.CHAR_NOTIFY),
                              ("WRITE_UUID", ble.CHAR_WRITE), ("APP_IDENTIFIER", commands.DEFAULT_IDENTIFIER),
                              ("PAIRING_PIN", commands.DEFAULT_PIN), ("CAMERA_HOST", transport.CAMERA_HOST)):
            self.assertEqual(constants[key], expected)
        self.assertRegex(self.direct, rf"CAMERA_TCP_PORT\s*=\s*{transport.CAMERA_TCP_POKE_PORT}\b")
        self.assertRegex(self.direct, rf"CAMERA_UDP_PORT\s*=\s*{transport.CAMERA_UDP_PORT}\b")

    def test_actual_handshake_agrees(self):
        block = re.search(r"uint8_t handshake\[[^]]*\]\s*=\s*\{(.*?)\};", self.direct, re.S)
        self.assertIsNotNone(block)
        payload = bytes(int(x, 0) for x in re.findall(r"0x[0-9A-Fa-f]+|\b\d+\b", block.group(1)))
        self.assertEqual(payload, transport.handshake_payload(0))

    def test_deadman_gate_agrees(self):
        self.assertIn("now - lastPacketMs_ > 2000", self.direct)
        self.assertEqual(datalink.LINK_SILENT_S, 2.0)

    def test_record_and_timelapse_paths_agree(self):
        self.assertIn('say("B tl_stop")', self.main)
        self.assertIn('elif name == "tl_stop":', self.server)
        self.assertIn('elif name == "record_stop":', self.server)
        self.assertIn('self.action("record_stop")', self.server)
        self.assertIn('rig.recordIntent = keyBool(s, "recint");', self.main)
        self.assertIn("recint=self.recording,", self.server)

    def test_every_action_the_firmware_emits_is_accepted(self):
        """The firmware sent record_start/record_stop for months while this
        set only knew "rec", so hold-to-record over USB was silently dropped
        at the trust boundary. Derive the emitted vocabulary from the source
        and check it here, so the two sides cannot drift apart again."""
        import re
        from pathlib import Path
        main = self.main
        emitted = set()
        expansions = {
            "record_%s": ("record_start", "record_stop"),
            "limitleds_%d": ("limitleds_0", "limitleds_1"),
            "beacon_%d": ("beacon_0", "beacon_1"),
            "invert_tilt_%d": ("invert_tilt_0", "invert_tilt_1"),
            "invert_pan_%d": ("invert_pan_0", "invert_pan_1"),
            "speed_%s": ("speed_SLOW", "speed_NORMAL", "speed_FAST"),
            "smooth_%s": ("smooth_CRISP", "smooth_FLUID", "smooth_GLIDE"),
            "jog_speed_%s": ("jog_speed_SLOW", "jog_speed_NORMAL", "jog_speed_FAST"),
            "jog_smooth_%s": ("jog_smooth_CRISP", "jog_smooth_FLUID", "jog_smooth_GLIDE"),
            "template_%s": ("template_follow", "template_rate"),
            "tilt_response_%s": ("tilt_response_FINE", "tilt_response_BALANCED",
                                  "tilt_response_DIRECT"),
            "pan_response_%s": ("pan_response_FINE", "pan_response_BALANCED",
                                 "pan_response_DIRECT"),
            "tilt_stability_%s": ("tilt_stability_QUIET", "tilt_stability_BALANCED",
                                   "tilt_stability_RESPONSIVE"),
            "pan_stability_%s": ("pan_stability_QUIET", "pan_stability_BALANCED",
                                  "pan_stability_RESPONSIVE"),
        }
        for name in re.findall(r'say\("B ([A-Za-z_%][A-Za-z0-9_%]*)', main):
            emitted.update(expansions.get(name, (name,)))
        self.assertTrue(emitted, "no B actions found in main.cpp")
        missing = sorted(emitted - core2.ACTIONS)
        self.assertEqual(missing, [], f"firmware emits actions the driver drops: {missing}")
