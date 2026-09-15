import math
import struct
import unittest

from driver.camera_feedback import CameraFeedback, parse_subscribe


def subscription(name, value):
    key = name.encode()
    return b'\x02\x06' + bytes(11) + struct.pack('<H', len(key)) + key + bytes(6) + struct.pack('<H', len(value)) + value


class CameraFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.feedback = CameraFeedback(clock=lambda: self.now)

    def test_flags_and_elapsed_are_camera_facts(self):
        p = bytearray(31)
        p[0] = 0x81
        p[29:31] = struct.pack('<H', 42)
        self.assertTrue(self.feedback.note(2, 0x80, p))
        self.assertEqual(self.feedback.snapshot()['recording']['value'], True)
        self.assertEqual(self.feedback.snapshot()['record_seconds']['value'], 42)
        p[0] = 1
        self.feedback.note(2, 0x80, p)
        self.assertIs(self.feedback.snapshot()['recording']['value'], False)

    def test_capture_mode_needs_its_own_fresh_byte_and_post_request_observation(self):
        p = bytearray(58); p[57] = 0x17
        self.feedback.note(2, 0x80, p)
        self.assertEqual(self.feedback.snapshot()['capture_mode']['value'], 'photo')
        self.assertFalse(self.feedback.matches_since('capture_mode', 'photo', 10.1))
        self.now = 12
        self.feedback.note(2, 0x80, bytes(31))
        self.now = 13
        self.assertFalse(self.feedback.snapshot()['capture_mode']['reported'])
        p[57] = 1
        self.feedback.note(2, 0x80, p)
        self.assertTrue(self.feedback.matches_since('capture_mode', 'video', 12))
        p[57] = 5
        self.feedback.note(2, 0x80, p)
        self.assertIsNone(self.feedback.snapshot()['capture_mode']['value'])
        p[3] = 0x40
        self.feedback.note(2, 0x80, p)
        self.assertTrue(self.feedback.snapshot()['playback']['value'])

    def test_all_documented_focus_bytes(self):
        for mode, expected in ((1, 'single'), (0xB1, 'single'), (2, 'continuous'), (0xB2, 'continuous')):
            p = bytes([mode]) + struct.pack('<ff', .25, .75) + bytes(5) + struct.pack('<H', 651)
            self.assertTrue(self.feedback.note(0, 0x99, subscription('cam_lens_state', p)))
            s = self.feedback.snapshot()
            self.assertEqual(s['focus_mode']['value'], expected)
            self.assertEqual(s['zoom']['value'], 3)
            self.assertEqual(s['focus_point']['value'], {'x': .25, 'y': .75})
            s['focus_point']['value']['x'] = 99
            self.assertEqual(self.feedback.snapshot()['focus_point']['value']['x'], .25)

    def test_color_and_timer_do_not_invent_recording(self):
        self.feedback.note(0, 0x99, subscription('cam_image_effect', b'\0\0\x41'))
        self.feedback.note(0, 0x99, subscription('cam_record_time', struct.pack('<I', 42)))
        s = self.feedback.snapshot()
        self.assertEqual(s['color']['value'], 'd-log2')
        self.assertEqual(s['record_seconds']['value'], 42)
        self.assertFalse(s['recording']['reported'])

    def test_truncation_lengths_and_ack_fail_closed(self):
        p = subscription('cam_lens_state', b'\xB2')
        for size in range(len(p)):
            self.assertIsNone(parse_subscribe(p[:size]))
        for command in (0x24, 0x30, 0xB8, 0x02):
            self.assertFalse(self.feedback.note(2, command, b'\0'))
        self.assertFalse(self.feedback.note(2, 0x80, bytes(12)))
        self.assertIsNone(parse_subscribe(b'\x02\x06' + bytes(11) + b'\xff\xff' + bytes(80)))
        self.assertFalse(any(r['reported'] for r in self.feedback.snapshot().values()))

    def test_invalid_lens_values_and_timestamp(self):
        p = b'\x09' + struct.pack('<ff', math.nan, 2) + bytes(5) + struct.pack('<H', 0)
        self.assertFalse(self.feedback.note(0, 0x99, subscription('cam_lens_state', p)))
        for now in (math.nan, math.inf, -1, 'bad'):
            self.assertFalse(self.feedback.note(2, 0x80, bytes(13), now=now))

    def test_expiry_order_and_invalid_clock(self):
        self.feedback.note(2, 0x80, bytes(13))
        self.assertFalse(self.feedback.note(2, 0x80, b'\x80' + bytes(12), now=9))
        self.assertFalse(self.feedback.snapshot()['recording']['value'])
        self.now = 12.999
        self.assertTrue(self.feedback.snapshot()['recording']['reported'])
        self.now = 13
        self.assertFalse(self.feedback.snapshot()['recording']['reported'])
        self.assertIsNone(self.feedback.snapshot()['recording']['value'])
        self.assertFalse(self.feedback.snapshot(now=math.nan)['recording']['reported'])
        for ttl in (0, -1, math.nan, math.inf, True):
            with self.assertRaises(ValueError):
                CameraFeedback(stale_after=ttl)

    def test_datalink_subscribes_to_optional_feedback_and_resets_it(self):
        from driver.datalink import Datalink
        from driver import commands
        link = Datalink()
        frames=[]
        link.send_frame=frames.append
        link.send_ack=lambda:None
        link._subscribe()
        self.assertEqual(len(frames),len(commands.SUBSCRIPTION_KEYS)+2)
        self.assertIn(b'cam_lens_state',frames[-2].payload)
        self.assertIn(b'cam_image_effect',frames[-1].payload)
        link.camera_feedback.note(2,0x80,bytes(31))
        link._reset_session()
        self.assertFalse(link.camera_feedback.snapshot()['recording']['reported'])
