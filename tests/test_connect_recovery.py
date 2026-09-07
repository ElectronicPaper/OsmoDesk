"""Recovery paths on the way to a connected camera.

Both of these run only after something has already gone wrong, which makes them
the code least likely to be exercised and most likely to be quietly broken. Both
were written in response to failures seen on the rig, not to hypotheticals:

* Windows serves its own cached copy of a peer's GATT table without consulting
  the device. When that copy goes stale the characteristics are simply absent
  and every connect fails from then on, while the camera sits there advertising
  normally. It was possible to prove the camera was innocent because an ESP32
  enumerated fff4 and fff5 on it minutes after Windows insisted fff4 did not
  exist.

* The access point sleeps whenever no client holds it. The BLE wake does not
  always have it beaconing before the join begins scanning, and losing that
  race failed the whole connect. On the rig it took three attempts once and two
  another time, so a single shot was never going to be enough.

A retry that silently does not retry looks exactly like one that does, right up
until the moment it matters.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from bleak.exc import BleakCharacteristicNotFoundError

from driver import ble as ble_mod


class TestStaleGattCacheRecovery(unittest.IsolatedAsyncioTestCase):
    def _link(self, fail_first: bool):
        """An OsmoBle whose _open fails once with the Windows cache error."""
        dev = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="OsmoPocket4P")
        with mock.patch.object(ble_mod, "BleakClient") as client_cls:
            client_cls.side_effect = lambda *a, **kw: mock.MagicMock(
                _kwargs=kw, disconnect=mock.AsyncMock())
            link = ble_mod.OsmoBle(dev)
        link._built = client_cls
        calls = []

        async def fake_open():
            calls.append(1)
            if fail_first and len(calls) == 1:
                raise BleakCharacteristicNotFoundError(ble_mod.CHAR_NOTIFY)

        link._open = fake_open
        link._calls = calls
        return link

    async def test_a_healthy_connect_does_not_rediscover(self):
        """Rediscovery costs a full service walk, so it must not be routine."""
        link = self._link(fail_first=False)
        with mock.patch.object(ble_mod, "BleakClient") as rebuilt:
            await link.__aenter__()
            rebuilt.assert_not_called()
        self.assertEqual(len(link._calls), 1)

    async def test_a_missing_characteristic_triggers_one_retry(self):
        link = self._link(fail_first=True)
        with mock.patch.object(ble_mod, "BleakClient") as rebuilt:
            rebuilt.return_value = mock.MagicMock()
            await link.__aenter__()
            rebuilt.assert_called_once()
            # The whole point: ask Windows to go and look again. Only Windows
            # caches services across connections, and only bleak's WinRT
            # backend accepts the switch -- passing it on Linux raises
            # TypeError, which would turn a recoverable retry into a hard
            # failure on the Pi. BlueZ rediscovers anyway, so the retry alone
            # is the fix there.
            if sys.platform == "win32":
                self.assertEqual(
                    rebuilt.call_args.kwargs.get("winrt"),
                    {"use_cached_services": False},
                    "the retry must force rediscovery, not repeat the cached read")
            else:
                self.assertNotIn("winrt", rebuilt.call_args.kwargs,
                                 "winrt is a Windows-backend option; passing "
                                 "it to BlueZ raises TypeError")
        self.assertEqual(len(link._calls), 2)

    async def test_a_second_failure_is_not_swallowed(self):
        """If rediscovery also fails, the caller has to hear about it."""
        dev = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="OsmoPocket4P")
        with mock.patch.object(ble_mod, "BleakClient") as client_cls:
            client_cls.return_value = mock.MagicMock(disconnect=mock.AsyncMock())
            link = ble_mod.OsmoBle(dev)

            async def always_fail():
                raise BleakCharacteristicNotFoundError(ble_mod.CHAR_NOTIFY)

            link._open = always_fail
            with self.assertRaises(BleakCharacteristicNotFoundError):
                await link.__aenter__()


class TestAccessPointWakeRetry(unittest.TestCase):
    def _session(self, skip_ble=False):
        import server
        args = SimpleNamespace(
            ssid="OsmoPocket4P-TEST", password="x", wifi_interface=None,
            skip_ble=skip_ble, skip_wifi_join=False, imu_port=None,
            no_core2=True, host="192.168.2.1", kp=2.0, port=8722,
        )
        return server.CameraSession.__new__(server.CameraSession), args

    def _run_join(self, failures, skip_ble=False):
        """Drive _join_ap with a join that fails `failures` times first."""
        import server
        sess, args = self._session(skip_ble)
        sess.args = args
        sess.stage = ""
        attempts = {"join": 0, "wake": 0}

        def fake_join(ssid, password, interface=None):
            attempts["join"] += 1
            if attempts["join"] <= failures:
                raise TimeoutError("never appeared in a scan")

        async def fake_creds(_args):
            attempts["wake"] += 1
            return "OsmoPocket4P-TEST", "x"

        with mock.patch.object(server.wifi, "join", fake_join), \
             mock.patch.object(server, "obtain_credentials", fake_creds):
            sess._join_ap("OsmoPocket4P-TEST", "x")
        return attempts

    def test_a_first_time_join_does_not_re_wake(self):
        a = self._run_join(failures=0)
        self.assertEqual(a["join"], 1)
        self.assertEqual(a["wake"], 0, "no need to disturb a point already up")

    def test_one_miss_is_recovered(self):
        a = self._run_join(failures=1)
        self.assertEqual(a["join"], 2)
        self.assertEqual(a["wake"], 1, "the wake has to be re-sent, not just rescanned")

    def test_two_misses_are_recovered(self):
        """Seen on the rig: it genuinely took three attempts once."""
        a = self._run_join(failures=2)
        self.assertEqual(a["join"], 3)

    def test_it_gives_up_rather_than_looping_forever(self):
        with self.assertRaises(TimeoutError):
            self._run_join(failures=99)

    def test_without_ble_there_is_nothing_to_wake_it_with(self):
        """--skip-ble means no way to send 0x53/0x10, so retrying the scan
        alone would just burn 25 seconds per attempt for nothing."""
        with self.assertRaises(TimeoutError):
            self._run_join(failures=99, skip_ble=True)


if __name__ == "__main__":
    unittest.main()
