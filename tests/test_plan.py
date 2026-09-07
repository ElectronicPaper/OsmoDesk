"""Flag-combination tests for the CLI connection plan.

`need_join` reassociates the machine's Wi-Fi adapter and drops it off the
network, so the cases that must NOT set it are the point of this file.
"""

import unittest

from driver.plan import JOIN_UNSUPPORTED, NO_CREDENTIALS, plan_connection

CREDS = {"ssid": "OsmoPocket4-ABC123", "password": "hunter2hunter2"}


class TestDefaultRun(unittest.TestCase):
    def test_bare_run_pairs_then_joins(self):
        p = plan_connection()
        self.assertTrue(p.ok)
        self.assertTrue(p.need_ble)
        self.assertTrue(p.need_join)
        self.assertFalse(p.stop_after_ble)

    def test_credentials_do_not_remove_the_need_for_ble(self):
        # BLE also sends the 0x53/0x10 AP wake. The camera's SoftAP sleeps when
        # no client holds it, so cached credentials alone leave nothing to join.
        # Skipping BLE here once cost a live join timeout.
        p = plan_connection(**CREDS)
        self.assertTrue(p.ok)
        self.assertTrue(p.need_ble)
        self.assertTrue(p.need_join)

    def test_only_explicit_skip_ble_drops_the_wake(self):
        p = plan_connection(**CREDS, skip_ble=True)
        self.assertTrue(p.ok)
        self.assertFalse(p.need_ble)
        self.assertTrue(p.need_join)


class TestPairOnly(unittest.TestCase):
    def test_pairs_and_stops(self):
        p = plan_connection(pair_only=True)
        self.assertTrue(p.ok)
        self.assertTrue(p.need_ble)
        self.assertTrue(p.stop_after_ble)
        self.assertFalse(p.need_join)

    def test_never_touches_wifi_whatever_else_is_set(self):
        for extra in (
            {},
            CREDS,
            {"skip_ble": True},
            {"skip_wifi_join": True},
            {"platform": "linux"},
            {**CREDS, "skip_ble": True, "platform": "darwin"},
        ):
            with self.subTest(extra=extra):
                p = plan_connection(pair_only=True, **extra)
                self.assertTrue(p.ok)
                self.assertFalse(p.need_join)
                self.assertTrue(p.stop_after_ble)

    def test_pairs_even_on_a_platform_that_cannot_join(self):
        # Reading credentials is useful on its own; no Wi-Fi work is implied.
        p = plan_connection(pair_only=True, platform="linux")
        self.assertTrue(p.ok)
        self.assertTrue(p.need_ble)


class TestAlreadyOnTheAccessPoint(unittest.TestCase):
    def test_skip_join_needs_nothing(self):
        p = plan_connection(skip_wifi_join=True)
        self.assertTrue(p.ok)
        self.assertFalse(p.need_ble)
        self.assertFalse(p.need_join)

    def test_skip_join_does_not_pair_just_because_an_ssid_was_given(self):
        # The old branching paired here, which was pointless: the credentials
        # are unused once you are associated.
        p = plan_connection(skip_wifi_join=True, ssid="OsmoPocket4-ABC123")
        self.assertTrue(p.ok)
        self.assertFalse(p.need_ble)
        self.assertFalse(p.need_join)

    def test_skip_join_works_off_windows(self):
        p = plan_connection(skip_wifi_join=True, platform="linux")
        self.assertTrue(p.ok)
        self.assertFalse(p.need_join)


class TestMissingCredentials(unittest.TestCase):
    def test_skip_ble_without_credentials_is_an_error(self):
        p = plan_connection(skip_ble=True)
        self.assertFalse(p.ok)
        self.assertEqual(p.error, NO_CREDENTIALS)
        self.assertFalse(p.need_join)

    def test_partial_credentials_are_not_credentials(self):
        for partial in ({"ssid": "x"}, {"password": "y"}, {"ssid": "", "password": "y"}):
            with self.subTest(partial=partial):
                p = plan_connection(skip_ble=True, **partial)
                self.assertFalse(p.ok)
                self.assertEqual(p.error, NO_CREDENTIALS)

    def test_partial_credentials_fall_back_to_ble(self):
        p = plan_connection(ssid="OsmoPocket4-ABC123")
        self.assertTrue(p.ok)
        self.assertTrue(p.need_ble)

    def test_every_join_that_can_wake_the_ap_does(self):
        # The invariant the live failure violated: if we intend to join and BLE
        # was not explicitly disabled, the AP wake must be planned.
        for creds in ({}, CREDS):
            with self.subTest(creds=bool(creds)):
                p = plan_connection(**creds)
                self.assertTrue(p.need_join)
                self.assertTrue(p.need_ble)


class TestPlatformGate(unittest.TestCase):
    def test_join_refused_off_windows(self):
        p = plan_connection(**CREDS, platform="linux")
        self.assertFalse(p.ok)
        self.assertEqual(p.error, JOIN_UNSUPPORTED)

    def test_refused_before_pairing_is_attempted(self):
        # Nobody should approve a prompt on the camera for a doomed run.
        p = plan_connection(platform="darwin")
        self.assertFalse(p.ok)
        self.assertFalse(p.need_ble)
        self.assertFalse(p.need_join)


class TestErrorPlansAreInert(unittest.TestCase):
    def test_no_action_flags_are_set_on_any_error(self):
        for kwargs in (
            {"skip_ble": True},
            {"platform": "linux"},
            {**CREDS, "platform": "linux"},
        ):
            with self.subTest(kwargs=kwargs):
                p = plan_connection(**kwargs)
                self.assertFalse(p.ok)
                self.assertFalse(p.need_ble)
                self.assertFalse(p.need_join)
                self.assertFalse(p.stop_after_ble)


class TestExhaustiveFlagMatrix(unittest.TestCase):
    """Every combination must be internally consistent."""

    def test_matrix(self):
        for pair_only in (False, True):
            for skip_ble in (False, True):
                for skip_wifi_join in (False, True):
                    for creds in ({}, CREDS):
                        for platform in ("win32", "linux"):
                            with self.subTest(
                                pair_only=pair_only, skip_ble=skip_ble,
                                skip_wifi_join=skip_wifi_join,
                                creds=bool(creds), platform=platform,
                            ):
                                self._check(plan_connection(
                                    pair_only=pair_only, skip_ble=skip_ble,
                                    skip_wifi_join=skip_wifi_join,
                                    platform=platform, **creds,
                                ), skip_ble=skip_ble, platform=platform,
                                    have_creds=bool(creds))

    def _check(self, p, *, skip_ble, platform, have_creds):
        if not p.ok:
            self.assertFalse(p.need_ble)
            self.assertFalse(p.need_join)
            self.assertFalse(p.stop_after_ble)
            return
        # Joining is only ever planned on a platform that can do it.
        if p.need_join:
            self.assertEqual(platform, "win32")
            # And only with credentials in hand, or a plan to fetch them.
            self.assertTrue(have_creds or p.need_ble)
        # --skip-ble is honoured except by --pair-only, which asks for it.
        if skip_ble and not p.stop_after_ble:
            self.assertFalse(p.need_ble)
        # Stopping after BLE implies doing BLE.
        if p.stop_after_ble:
            self.assertTrue(p.need_ble)


if __name__ == "__main__":
    unittest.main()
