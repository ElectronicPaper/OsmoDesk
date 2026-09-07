"""Decide what the CLI has to do before it touches any hardware.

Pure function, no I/O. The branching it replaces was subtle enough that it had
to be traced by hand to be believed, and one of its outcomes reassociates the
machine's Wi-Fi adapter -- worth being able to assert on.
"""

from __future__ import annotations

from dataclasses import dataclass

NO_CREDENTIALS = (
    "no Wi-Fi credentials -- drop --skip-ble so they can be read over BLE, "
    "or pass --ssid/--password"
)
JOIN_UNSUPPORTED = (
    "automatic SoftAP join is Windows-only here -- join the camera network "
    "manually and re-run with --skip-wifi-join"
)


@dataclass(frozen=True)
class ConnectionPlan:
    need_ble: bool = False
    """Run BLE discovery, app-pairing and the Wi-Fi credential read."""

    stop_after_ble: bool = False
    """--pair-only: print the credentials and exit before any Wi-Fi work."""

    need_join: bool = False
    """Associate to the camera SoftAP."""

    error: str | None = None
    """Set when the flag combination cannot work. Nothing else should run."""

    @property
    def ok(self) -> bool:
        return self.error is None


def plan_connection(
    *,
    pair_only: bool = False,
    skip_ble: bool = False,
    skip_wifi_join: bool = False,
    ssid: str | None = None,
    password: str | None = None,
    platform: str = "win32",
) -> ConnectionPlan:
    if pair_only:
        # Pairing alone needs no Wi-Fi, so no platform or credential checks apply.
        return ConnectionPlan(need_ble=True, stop_after_ble=True, need_join=False)

    have_creds = bool(ssid and password)
    need_join = not skip_wifi_join

    # Refuse before pairing rather than after, so nobody is asked to approve a
    # prompt on the camera for a run that cannot finish.
    if need_join and platform != "win32":
        return ConnectionPlan(error=JOIN_UNSUPPORTED)

    # BLE is not just the credential read: 0x53/0x10 is what wakes the camera's
    # access point, and it sleeps as soon as the last client leaves. So any run
    # that intends to join needs the BLE leg, cached credentials or not. Having
    # them only saves the read, never the wake.
    need_ble = need_join and not skip_ble

    if need_join and not have_creds and not need_ble:
        return ConnectionPlan(error=NO_CREDENTIALS)

    return ConnectionPlan(need_ble=need_ble, need_join=need_join)
