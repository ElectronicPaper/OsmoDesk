"""Transport-window ACK handling stays split by camera stream."""

import time
import unittest

from driver import transport
from driver.datalink import Datalink


def packet(pkt_type: int, seq: int, length: int = 34) -> bytes:
    """A camera-shaped datagram with a chosen transport sequence."""
    data = bytearray(transport.transport_header(pkt_type, length - 8, 1, seq))
    data.extend(b"\x00" * (length - 8))
    return data


class TestDatalinkAckWindows(unittest.TestCase):
    def setUp(self):
        self.link = Datalink(tcp_poke=False)
        self.link.base_seq = 0x1238
        self.link.ack_windows = transport.AckWindows.for_handshake(self.link.base_seq)
        self.link._inbound_logged = 6
        self.link.video_packets = 0
        self.link.live = None

    def test_each_inbound_stream_advances_only_its_own_ack_group(self):
        video = packet(transport.PKT_VIDEO, 0x2008, 16)
        reliable = packet(transport.PKT_ACKED_DATA, 0x2010, 16)
        telemetry = packet(transport.PKT_TELEMETRY, 0x2018)
        telemetry[26:28] = b"\x20\x20"

        self.link._handle_datagram(video)
        self.assertEqual(self.link.ack_windows,
                         transport.AckWindows(0x2008, 0, 0))
        self.link._handle_datagram(reliable)
        self.assertEqual(self.link.ack_windows,
                         transport.AckWindows(0x2008, 0x2010, 0))
        self.link._handle_datagram(telemetry)
        self.assertEqual(self.link.ack_windows,
                         transport.AckWindows(0x2008, 0x2010, 0x2020))

    def test_reliable_data_window_does_not_stay_at_base_sequence(self):
        self.link._handle_datagram(packet(transport.PKT_ACKED_DATA, 0x3480, 16))
        payload = transport.ack_payload(self.link.ack_windows)
        self.assertEqual(payload[8:12], b"\x80\x34\x80\x34")
        self.assertNotEqual(payload[8:10], b"\x38\x12")

    def test_send_ack_serializes_all_three_independent_groups(self):
        class RecordingSocket:
            def __init__(self):
                self.sent = []

            def send(self, data):
                self.sent.append(data)

        self.link.sock = RecordingSocket()
        self.link.session_id = 0x1111
        self.link.udp_seq = 0x4400
        self.link.ack_windows = transport.AckWindows(0x2008, 0x2010, 0x2018)
        self.link.send_ack()

        sent = self.link.sock.sent[0]
        self.assertEqual(sent[4:6], b"\x00\x00")
        self.assertEqual(sent[8:12], b"\x08\x20\x08\x20")
        self.assertEqual(sent[16:20], b"\x10\x20\x10\x20")
        self.assertEqual(sent[24:28], b"\x18\x20\x18\x20")
        self.assertEqual(self.link.udp_seq, 0x4400)

    def test_telemetry_seed_only_applies_when_reliable_window_is_zero(self):
        telemetry = packet(transport.PKT_TELEMETRY, 0x2008)
        telemetry[18:20] = b"\x70\x23"
        telemetry[26:28] = b"\x80\x23"
        self.link._handle_datagram(telemetry)
        self.assertEqual(self.link.ack_windows,
                         transport.AckWindows(0, 0x2370, 0x2380))

    def test_first_telemetry_replaces_wire_fallback_then_pkt3_stays_authoritative(self):
        initial = transport.ack_payload(self.link.ack_windows, self.link.base_seq)
        self.assertEqual(initial[8:10], b"\x38\x12")
        self.assertEqual(initial[16:18], b"\x38\x12")

        telemetry = packet(transport.PKT_TELEMETRY, 0x2008)
        telemetry[18:20] = b"\x70\x23"
        telemetry[26:28] = b"\x80\x23"
        self.link._handle_datagram(telemetry)
        seeded = transport.ack_payload(self.link.ack_windows, self.link.base_seq)
        self.assertEqual(seeded[8:10], b"\x70\x23")
        self.assertEqual(seeded[16:18], b"\x80\x23")

        self.link._handle_datagram(packet(transport.PKT_ACKED_DATA, 0x3480, 16))
        telemetry[18:20] = b"\x90\x23"
        self.link._handle_datagram(telemetry)
        self.assertEqual(self.link.ack_windows.acked_data, 0x3480)

    def test_short_telemetry_cannot_mutate_camera_or_ack_windows(self):
        short = packet(transport.PKT_TELEMETRY, 0x2008, 20)
        short[8:10] = b"\x08\x30"
        self.link._handle_datagram(short)
        self.assertEqual(self.link.cam_channel, 0)
        self.assertFalse(self.link._channel_seen.is_set())
        self.assertEqual(self.link.ack_windows,
                         transport.AckWindows())



class TestLinkHealth(unittest.TestCase):
    """The link had no notion of being dead: attitude is last-known and never
    cleared, so a sleeping camera stayed "connected" forever."""

    def test_silence_is_unhealthy_and_a_datagram_is_health(self):
        from driver import datalink
        link = datalink.Datalink.__new__(datalink.Datalink)
        link.sock = object()
        link.last_rx_at = 0.0
        self.assertFalse(link.healthy)
        link.last_rx_at = time.monotonic()
        self.assertTrue(link.healthy)
        link.last_rx_at = time.monotonic() - datalink.LINK_SILENT_S - 0.5
        self.assertFalse(link.healthy)
        link.sock = None
        link.last_rx_at = time.monotonic()
        self.assertFalse(link.healthy)

    def test_the_gate_matches_the_wire_contract(self):
        import json
        from driver import datalink
        from pathlib import Path
        wire = json.loads((Path(__file__).resolve().parents[1] /
                           "contracts/camera-control-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(datalink.LINK_SILENT_S, wire["link_silent_s"])


if __name__ == "__main__":
    unittest.main()
