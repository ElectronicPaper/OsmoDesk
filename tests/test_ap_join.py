"""Joining the camera's access point, which does not always come up in time.

The point sleeps whenever no client holds it. The BLE leg sends a wake, then
the join starts scanning, and the two race. Losing that race is not a fault in
anything -- the camera is fine, the credentials are fine -- but a single
attempt turns it into a failed connect. It was watched happening three times in
one afternoon, and a second attempt has always worked.

`_join_ap` retries by re-running the BLE leg, because that is what re-sends the
wake and the first session has already been closed by the time the join runs.
These pin the parts that are easy to get subtly wrong: that it does retry, that
it eventually gives up, that it does not retry when there is no BLE to wake
with, and that a failure inside the re-wake still surfaces the join error
rather than burying it.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import server


def make_args(**over):
    base = dict(ssid="OsmoPocket4-TEST-CAMERA", password="pw", skip_ble=False,
                skip_wifi_join=False, wifi_interface=None, host="192.168.2.1",
                gain=1.0, pin="osmo", name=None, scan_timeout=1.0,
                kp=2.0, no_live_view=True, no_core2=True, imu_port=None)
    base.update(over)
    return SimpleNamespace(**base)


class JoinHarness:
    """Counts joins and wakes, and lets the test choose which joins fail."""

    def __init__(self, fail_first=0, wake_raises=False):
        self.joins = 0
        self.wakes = 0
        self.fail_first = fail_first
        self.wake_raises = wake_raises

    def join(self, ssid, password, interface=None):
        self.joins += 1
        if self.joins <= self.fail_first:
            raise TimeoutError(
                f"{ssid} never appeared in a scan within 25.0s")

    async def obtain_credentials(self, args):
        # A coroutine, because the caller hands it to asyncio.run(). A plain
        # function here fails inside the recovery path instead of the join,
        # which makes every retry look broken.
        self.wakes += 1
        if self.wake_raises:
            raise RuntimeError("BLE went away")
        return ("OsmoPocket4-TEST-CAMERA", "pw")


class ApJoinTest(unittest.TestCase):
    def run_join(self, harness, **args):
        session = server.CameraSession(make_args(**args))
        with mock.patch.object(server.wifi, "join", harness.join), \
             mock.patch.object(server, "obtain_credentials",
                               harness.obtain_credentials):
            session._join_ap("OsmoPocket4-TEST-CAMERA", "pw")
        return session


class TestItRetries(ApJoinTest):
    def test_a_first_time_success_does_not_wake_anything(self):
        """The retry must not cost a BLE round trip on the normal path."""
        h = JoinHarness(fail_first=0)
        self.run_join(h)
        self.assertEqual((h.joins, h.wakes), (1, 0))

    def test_a_missed_access_point_is_woken_and_retried(self):
        """The whole reason this exists."""
        h = JoinHarness(fail_first=1)
        self.run_join(h)
        self.assertEqual(h.joins, 2)
        self.assertEqual(h.wakes, 1, "the wake is what makes a retry worth it")

    def test_it_survives_two_misses(self):
        h = JoinHarness(fail_first=2)
        self.run_join(h)
        self.assertEqual(h.joins, 3)


class TestItGivesUp(ApJoinTest):
    def test_a_point_that_never_comes_up_raises(self):
        """Retrying for ever would hang the connect instead of failing it."""
        h = JoinHarness(fail_first=99)
        with self.assertRaises(TimeoutError):
            self.run_join(h)

    def test_it_stops_after_a_bounded_number_of_tries(self):
        h = JoinHarness(fail_first=99)
        with self.assertRaises(TimeoutError):
            self.run_join(h)
        self.assertLessEqual(h.joins, 4, "unbounded retrying is a hang")


class TestNothingToWakeWith(ApJoinTest):
    def test_skip_ble_does_not_retry(self):
        """Without the BLE leg there is no way to send the wake, so a retry is
        just the same scan again -- slower, and no likelier to work."""
        h = JoinHarness(fail_first=99)
        with self.assertRaises(TimeoutError):
            self.run_join(h, skip_ble=True)
        self.assertEqual(h.joins, 1)
        self.assertEqual(h.wakes, 0)


class TestAFailedWake(ApJoinTest):
    def test_the_join_error_is_the_one_reported(self):
        """If the re-wake also fails, the useful message is still the one about
        the access point -- not a secondary BLE error from the recovery path."""
        h = JoinHarness(fail_first=99, wake_raises=True)
        with self.assertRaises(TimeoutError) as caught:
            self.run_join(h)
        self.assertIn("never appeared in a scan", str(caught.exception))

    def test_it_does_not_keep_trying_after_the_wake_dies(self):
        h = JoinHarness(fail_first=99, wake_raises=True)
        with self.assertRaises(TimeoutError):
            self.run_join(h)
        self.assertEqual(h.joins, 1)


if __name__ == "__main__":
    unittest.main()
