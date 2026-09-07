"""The Core2 cable can be pulled, and the link has to come back.

The reported fault: unplug the Core2 and plug it in again, and it never comes
back online for the rest of the session. The cause was that the reader thread
`return`ed the first time a read raised. The thread was then gone, nothing
owned the port, and no code path anywhere reopened it -- so replugging was
not merely slow to recover, it could not recover at all.

These drive a fake serial port through the ways a USB cable actually fails,
including the Windows one where the handle survives the unplug and reads just
return nothing forever, which looks identical to a working link.
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import serial

from driver import core2


class FakeSerial:
    """A serial port that can be told how to die."""

    def __init__(self, port, baud, timeout=0.2):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.closed = False
        self.written: list[bytes] = []
        self.lines: list[bytes] = []
        self.mode = "silent"          # silent | lines | raise
        self._lock = threading.Lock()

    def feed(self, text: str) -> None:
        with self._lock:
            self.lines.append((text + "\n").encode())
            self.mode = "lines"

    def readline(self):
        if self.mode == "raise":
            raise serial.SerialException("device disconnected")
        with self._lock:
            if self.lines:
                return self.lines.pop(0)
        time.sleep(0.005)             # stand in for the read timeout
        return b""

    def write(self, payload):
        if self.closed:
            raise serial.SerialException("write to closed port")
        self.written.append(payload)
        return len(payload)

    def close(self):
        self.closed = True


class LinkHarness:
    """Runs a real Core2Link against fake ports, with the waits shortened."""

    def __init__(self, ports=("COM7",), requested=None):
        self.available = list(ports)
        self.opened: list[FakeSerial] = []
        self.open_failures = 0
        self._patches = []
        self.link = None
        self._requested = requested

    def _find_port(self):
        return self.available[0] if self.available else None

    def _serial_factory(self, port, baud, timeout=0.2):
        if port not in self.available:
            self.open_failures += 1
            raise serial.SerialException(f"cannot open {port}")
        s = FakeSerial(port, baud, timeout)
        self.opened.append(s)
        return s

    def __enter__(self):
        self._patches = [
            mock.patch.object(core2, "find_port", self._find_port),
            mock.patch.object(core2.serial, "Serial", self._serial_factory),
            mock.patch.object(core2, "RECONNECT_DELAY_S", 0.02),
            mock.patch.object(core2, "SILENCE_TIMEOUT_S", 0.15),
        ]
        for p in self._patches:
            p.start()
        self.link = core2.Core2Link(self._requested)
        return self

    def __exit__(self, *exc):
        if self.link:
            self.link.stop()
        for p in reversed(self._patches):
            p.stop()

    def wait_for(self, predicate, timeout=3.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")


class TestReconnectAfterUnplug(unittest.TestCase):
    def test_a_read_error_does_not_end_the_link(self):
        """The whole reported bug in one assertion."""
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.wait_for(lambda: h.link.status.connected, what="connected")

            h.opened[0].mode = "raise"          # the cable comes out
            h.wait_for(lambda: len(h.opened) >= 2, what="reopen after unplug")
            h.wait_for(lambda: h.link.status.connected, what="reconnected")
            self.assertTrue(h.opened[0].closed, "the dead handle must be closed")

    def test_it_keeps_trying_while_the_port_is_gone(self):
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.available.clear()                 # unplugged, nothing enumerated
            h.opened[0].mode = "raise"
            h.wait_for(lambda: not h.link.status.connected, what="disconnect")
            time.sleep(0.15)
            self.assertEqual(len(h.opened), 1, "nothing to open yet")

            h.available.append("COM7")          # plugged back in
            h.wait_for(lambda: len(h.opened) >= 2, what="reopen after replug")
            self.assertTrue(h.link.status.connected)

    def test_a_different_com_number_is_rediscovered(self):
        """Windows is free to renumber the port on replug. Reopening the
        remembered name would then fail forever."""
        with LinkHarness(ports=("COM7",)) as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.opened[0].mode = "raise"
            h.available[:] = ["COM11"]          # same box, new number
            h.wait_for(lambda: len(h.opened) >= 2, what="reopen on the new port")
            self.assertEqual(h.opened[-1].port, "COM11")
            self.assertEqual(h.link.status.port, "COM11")

    def test_an_explicitly_requested_port_is_not_second_guessed(self):
        """If the operator pinned a port, wandering off to another device is
        worse than waiting for theirs to come back."""
        with LinkHarness(ports=("COM7",), requested="COM7") as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.opened[0].mode = "raise"
            h.available[:] = ["COM11"]          # some other adapter appears
            time.sleep(0.2)
            self.assertTrue(all(s.port == "COM7" for s in h.opened),
                            "must not hop to a port nobody asked for")


class TestSilentPort(unittest.TestCase):
    """On Windows an unplugged USB serial adapter often does not raise: the
    handle stays valid and reads return nothing. The box streams attitude at
    about 100 Hz, so silence is the only signal there is."""

    def test_silence_is_treated_as_a_dead_link(self):
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            # No feeding at all, and no exception either.
            h.wait_for(lambda: len(h.opened) >= 2, what="reopen after silence")

    def test_a_talking_box_is_never_dropped(self):
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            end = time.time() + 0.6
            while time.time() < end:
                h.opened[0].feed("I 1.0 0.0 0.0 0.0")
                time.sleep(0.02)
            self.assertEqual(len(h.opened), 1,
                             "a live link must not be reconnected under us")


class TestStateOnDrop(unittest.TestCase):
    def test_the_firmware_string_is_forgotten(self):
        """Whatever is plugged in next may not be running what the last one
        was, and a stale version number is worse than none."""
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.opened[0].feed("H 2.4-clutch imu,touch")
            h.wait_for(lambda: h.link.status.firmware == "2.4-clutch",
                       what="hello")
            h.available.clear()
            h.opened[0].mode = "raise"
            h.wait_for(lambda: not h.link.status.connected, what="disconnect")
            self.assertEqual(h.link.status.firmware, "")
            self.assertEqual(h.link.status.features, set())

    def test_a_held_clutch_is_released(self):
        """A clutch held at the instant the cable went is not held any more,
        and the head must not keep being driven on the strength of it."""
        seen = []
        with LinkHarness() as h:
            h.link.on_engage = seen.append
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.opened[0].feed("E 1")
            h.wait_for(lambda: seen == [True], what="engage")
            h.available.clear()
            h.opened[0].mode = "raise"
            h.wait_for(lambda: seen[-1] is False, what="release on disconnect")

    def test_state_is_resent_in_full_after_a_reconnect(self):
        """send_state suppresses unchanged lines, so without clearing the
        memo the reconnected box would sit with a blank screen until something
        happened to change."""
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.link.send_state(armed=True)
            h.opened[0].mode = "raise"
            h.wait_for(lambda: len(h.opened) >= 2, what="reopen")
            h.link.send_state(armed=True)       # identical to before the drop
            h.wait_for(lambda: any(b"armed=1" in w for w in h.opened[-1].written),
                       what="state resent after reconnect")


class TestLifecycle(unittest.TestCase):
    def test_start_does_not_raise_without_a_box(self):
        """It used to. That also meant a Core2 plugged in later was never
        picked up, because attach only ran once at connect time."""
        with LinkHarness(ports=()) as h:
            h.link.start()                       # must not raise
            time.sleep(0.1)
            self.assertFalse(h.link.status.connected)

    def test_stop_ends_the_supervisor_promptly(self):
        with LinkHarness(ports=()) as h:
            h.link.start()
            time.sleep(0.05)
            t0 = time.time()
            h.link.stop()
            self.assertLess(time.time() - t0, 2.0)
            self.assertFalse(h.link._thread)

    def test_sending_across_a_reconnect_gap_never_raises(self):
        with LinkHarness() as h:
            h.link.start()
            h.wait_for(lambda: len(h.opened) == 1, what="first open")
            h.available.clear()
            h.opened[0].mode = "raise"
            h.wait_for(lambda: not h.link.status.connected, what="disconnect")
            for _ in range(20):                  # the feed keeps pushing state
                h.link.send("A 1.0 2.0")
                h.link.vibrate(10)


if __name__ == "__main__":
    unittest.main()


class TestFindingTheBoard(unittest.TestCase):
    """The board is found by what it IS, not by what the operating system
    decided to call it. On Linux a port's description is frequently the
    literal string "n/a", and a build that only matched descriptions found
    the Core2 on Windows and not on the Pi."""

    def _ports(self, *specs):
        made = []
        for device, desc, vid, pid in specs:
            made.append(SimpleNamespace(device=device, description=desc,
                                        manufacturer=None, hwid=desc,
                                        vid=vid, pid=pid))
        return made

    def _find(self, ports):
        with mock.patch.object(core2.list_ports, "comports", return_value=ports):
            return core2.find_port()

    def test_the_measured_core2_bridge_is_found_by_id(self):
        self.assertEqual(
            self._find(self._ports(("/dev/ttyUSB0", "n/a", 0x10C4, 0xEA60))),
            "/dev/ttyUSB0")

    def test_a_nameless_port_is_still_found(self):
        """This is the Pi case exactly."""
        ports = self._ports(("/dev/ttyAMA10", "n/a", None, None),
                            ("/dev/ttyUSB0", "n/a", 0x10C4, 0xEA60))
        self.assertEqual(self._find(ports), "/dev/ttyUSB0")

    def test_the_pis_own_uart_is_not_mistaken_for_the_board(self):
        self.assertIsNone(
            self._find(self._ports(("/dev/ttyAMA10", "n/a", None, None))))

    def test_a_description_only_match_still_works(self):
        """A bridge revision not in the table should still be found, just
        less certainly."""
        self.assertEqual(
            self._find(self._ports(("COM7", "CH9102 USB to UART", None, None))),
            "COM7")

    def test_device_id_wins_over_a_misleading_description(self):
        ports = self._ports(("COM1", "CP210x lookalike", None, None),
                            ("COM9", "n/a", 0x1A86, 0x55D4))
        self.assertEqual(self._find(ports), "COM9")

    def test_nothing_plugged_in_is_none_not_a_crash(self):
        self.assertIsNone(self._find([]))
