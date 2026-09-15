"""Camera feedback and continuous scheduling: no physical camera required."""
import struct
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import server
from driver import camera, moves, timelapse
from driver.camera_feedback import CameraFeedback
from tests.test_camera_feedback import subscription
from tests.test_server import make_args, FakeLink, FakeStick, fake_runner


class CameraCompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.s = server.CameraSession(make_args(), workspace_root=Path(self.directory.name))
        self.s.link, self.s.stick = FakeLink(), FakeStick()
        self.s.link.healthy = True
        self.s.link.attitude = SimpleNamespace(pitch=0, yaw=0)
        self.s.runner = fake_runner(fault='')
        self.s.armed, self.s.owner = True, 'program'
        self.s.link.camera_feedback = CameraFeedback()
        self.feedback = self.s.link.camera_feedback
        self.feedback.note(2, 0x80, bytes(31))
        self.feedback.note(0, 0x99, subscription('cam_lens_state', b'\xB2' + struct.pack('<ff', .5, .5) + bytes(5) + struct.pack('<H', 217)))
        self.feedback.note(0, 0x99, subscription('cam_image_effect', b'\0\0\x3f'))
        self.s.move = moves.Move(waypoints=[moves.Waypoint('a',0,0), moves.Waypoint('b',0,1,duration=1)])

    def test_refocus_requires_ack_and_exact_documented_lengths(self):
        for ack in (False, None, 'true', 1):
            with self.assertRaises(ValueError):
                self.s.camera_focus_target(.2, .3, ack)
        self.assertEqual(self.s.link.frames, [])
        self.s.camera_focus_target(.2, .3, True)
        self.assertEqual([(f.cmd_id,len(f.payload)) for f in self.s.link.frames], [(0x22,1),(0x30,21),(0x68,1),(0x32,20)])
        for value in (True, -1, float('nan'), '0.5'):
            with self.assertRaises(ValueError):
                camera.focus_target_burst(value, .5)

    def test_manual_zoom_and_program_gates(self):
        self.s.camera_set('zoom', 1.2)
        self.assertEqual(self.s.link.frames[0].payload, b'\x0a\x4e' + struct.pack('<H', 260))
        self.assertEqual(camera.set_zoom(1).payload, b'\x0a\x4e\xd9\x00')
        self.assertEqual(camera.set_zoom(12).payload, b'\x0a\x4e\x2c\x0a')
        self.s.runner.running = True
        with self.assertRaises(RuntimeError): self.s.camera_set('zoom', 2)
        self.s.runner.running = False
        self.feedback.note(0, 0x99, subscription('cam_image_effect', b'\0\0\x41'))
        with self.assertRaisesRegex(RuntimeError, 'D-Log2'): self.s._program_zoom(.1)
        self.s.armed = False
        with self.assertRaises(RuntimeError): self.s._program_zoom(.1)
        self.assertEqual(len(self.s.link.frames), 1)

    def test_failed_runner_never_reaches_photo(self):
        self.s.runner.fault = 'telemetry lost'
        self.s.runner.report.aborted = True
        self.assertFalse(self.s._wait_for_runner(.1))
        plan=timelapse.plan_for(self.s.move,2,settle_s=0,expose_s=.1,gap_s=.1)
        self.s.tl_state.update(running=True,frames=2)
        self.s._timelapse_worker([(0,0),(0,1)],plan)
        self.assertEqual(self.s.link.frames, [])
        self.assertIn('did not arrive',self.s.tl_state['error'])

    def test_edits_blocked_between_hops(self):
        self.s.tl_state['running'] = True
        with self.assertRaises(RuntimeError): self.s.set_move(self.s.move.to_dict())

    def test_capture_requires_standby_and_disk_failure_stops(self):
        plan=timelapse.plan_for(self.s.move,2)
        self.feedback.note(2,0x80,b'\x80'+bytes(30))
        with self.assertRaises(RuntimeError): self.s._timelapse_capture(self.s.move,plan,1)
        self.assertEqual(self.s.link.frames,[])
        self.feedback.note(2,0x80,bytes(31))
        with patch.object(timelapse,'save_progress',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.s._timelapse_capture(self.s.move,plan,1)

    def test_continuous_single_runner_no_hop_and_exact_request_count(self):
        plan=timelapse.plan_for(self.s.move,3,expose_s=.1,gap_s=.1,mode='continuous')
        path=timelapse.continuous_plan_move(self.s.move,plan)
        starts=[]
        def start(m): starts.append(m); self.s.runner.running=True
        self.s.runner.start=start
        def tick(seconds): self.s.runner.elapsed += seconds; return False
        self.s._sleep_or_stop=tick
        self.s.tl_state.update(running=True,frames=3)
        self.s._continuous_timelapse_worker(path,self.s.move,plan)
        self.assertEqual(len(starts),1)
        self.assertEqual(self.s.tl_state['frame'],3)
        self.assertIsNone(self.s.tl_state['error'])
        self.assertEqual(len(self.s.link.frames),3)
        self.assertFalse(self.s.tl_state['running'])

    def test_continuous_late_tick_stops_without_burst(self):
        plan=timelapse.plan_for(self.s.move,3,expose_s=.1,gap_s=.1,mode='continuous')
        path=timelapse.continuous_plan_move(self.s.move,plan)
        self.s.runner.start=lambda m:setattr(self.s.runner,'running',True)
        def stall(seconds): self.s.runner.elapsed += 1; return False
        self.s._sleep_or_stop=stall
        self.s.tl_state.update(running=True,frames=3)
        self.s._continuous_timelapse_worker(path,self.s.move,plan)
        self.assertEqual(len(self.s.link.frames),1)
        self.assertIn('deadline missed',self.s.tl_state['error'])

    def test_stopped_capture_does_not_send(self):
        self.s._tl_stop.set()
        plan=timelapse.plan_for(self.s.move,2)
        self.assertFalse(self.s._timelapse_capture(self.s.move,plan,1))
        self.assertEqual(self.s.link.frames,[])

    def test_delayed_start_after_stop_cannot_restart_runner(self):
        starts=[]
        self.s.runner.start=starts.append
        self.s.stop_everything()
        self.assertFalse(self.s._timelapse_run(self.s.move))
        self.assertEqual(starts,[])

    def test_timelapse_owns_runner_against_other_routes(self):
        self.s.tl_state['running']=True
        for action in (lambda:self.s.goto(0),lambda:self.s.play_segment(0,1),lambda:self.s.play()):
            with self.assertRaisesRegex(RuntimeError,'timelapse'): action()

    def test_final_fault_and_countdown_settings_gate(self):
        self.s.runner.report.aborted=True
        with self.assertRaises(RuntimeError):
            self.s._timelapse_capture(self.s.move,timelapse.plan_for(self.s.move,2),1)
        self.s.preroll_until=10
        for what,value in (('zoom',1),('color','normal'),('focus_continuous',True)):
            with self.assertRaisesRegex(RuntimeError,'countdown'): self.s.camera_set(what,value)
        self.assertEqual(self.s.link.frames,[])

    def test_slow_recovery_storage_cannot_block_stop(self):
        entered,release,stopped=threading.Event(),threading.Event(),threading.Event()
        def save(*args): entered.set(); release.wait(2)
        def stop(): self.s.stop_everything(); stopped.set()
        with patch.object(timelapse,'save_progress',side_effect=save):
            worker=threading.Thread(target=self.s._timelapse_capture,args=(self.s.move,timelapse.plan_for(self.s.move,2),1))
            worker.start()
            stopper=threading.Thread(target=stop)
            try:
                self.assertTrue(entered.wait(1))
                stopper.start()
                self.assertTrue(stopped.wait(.5),'STOP waited for disk write')
            finally:
                release.set();worker.join(2)
                if stopper.ident: stopper.join(2)
        self.assertFalse(self.s.armed)
