"""Public wire constants, independent of a firmware checkout."""
import json
from pathlib import Path
import unittest
from driver import ble, commands, transport

ROOT = Path(__file__).resolve().parents[1]
WIRE = json.loads((ROOT / "contracts/camera-control-v1.json").read_text(encoding="utf-8"))


class TestWireContract(unittest.TestCase):
    def test_ble_identity(self):
        self.assertEqual(ble.SERVICE_UUID, WIRE["service_uuid"])
        self.assertEqual(ble.CHAR_NOTIFY, WIRE["notify_uuid"])
        self.assertEqual(ble.CHAR_WRITE, WIRE["write_uuid"])
        self.assertEqual(commands.DEFAULT_IDENTIFIER, WIRE["app_identifier"])
        self.assertEqual(commands.DEFAULT_PIN, WIRE["pairing_pin"])

    def test_transport_and_subscriptions(self):
        self.assertEqual(transport.CAMERA_HOST, WIRE["camera_host"])
        self.assertEqual(transport.CAMERA_TCP_POKE_PORT, WIRE["tcp_port"])
        self.assertEqual(transport.CAMERA_UDP_PORT, WIRE["udp_port"])
        self.assertEqual(transport.handshake_payload(0).hex(), WIRE["handshake"])
        self.assertEqual(commands.SUBSCRIPTION_KEYS, WIRE["subscription_keys"])
        self.assertEqual(commands.FIRST_SUB_ID, WIRE["first_sub_id"])

    def test_host_handles_panel_record_and_timelapse_intents(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('elif name == "tl_stop":', source)
        self.assertIn('elif name == "record_stop":', source)
        self.assertIn('self.action("record_stop")', source)
        self.assertIn("recint=self.recording,", source)
