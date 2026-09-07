"""Join the camera SoftAP on Linux via NetworkManager.

The same job `wifi_win` does with netsh, done with nmcli, so the rig can run
on a Raspberry Pi strapped to the tripod instead of a laptop on a stand. The
two modules present the same names and the same behaviour; `driver.wifi`
picks between them.

nmcli is the right tool rather than raw wpa_supplicant: it owns the interface
on a stock Pi OS, and fighting it for control produces a connection that comes
up once and is torn down the next time NetworkManager rescans.

One deliberate difference from a hand-rolled connection: the camera's AP has
no route to the internet, and NetworkManager will happily make it the default
route and blackhole everything else. The profile is created with
`ipv4.never-default yes` so the Pi keeps its ethernet route while it is talking
to the camera -- which is what makes it possible to stay logged in over SSH
while the rig is running.
"""

from __future__ import annotations

import shutil
import subprocess
import time

# The profile name. Reused so repeated joins update one profile rather than
# leaving a numbered pile behind after a week of shooting.
PROFILE = "osmo-pocket"

TIMEOUT_S = 25.0


def available() -> bool:
    """True if nmcli is present. The caller decides what to do about it."""
    return shutil.which("nmcli") is not None


def _nmcli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True,
                          timeout=timeout)


def _require_nmcli() -> None:
    if not available():
        raise RuntimeError(
            "nmcli is not installed -- this build joins the camera access "
            "point through NetworkManager"
        )


def list_interfaces() -> list[str]:
    """Wi-Fi device names, in nmcli's order."""
    _require_nmcli()
    res = _nmcli("-t", "-f", "DEVICE,TYPE", "device")
    out = []
    for line in res.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            out.append(parts[0])
    return out


def current_ssid(interface: str | None = None) -> str | None:
    """The SSID this machine is on, or None."""
    _require_nmcli()
    res = _nmcli("-t", "-f", "ACTIVE,SSID,DEVICE", "device", "wifi")
    for line in res.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "yes":
            if interface and parts[2] != interface:
                continue
            return parts[1] or None
    return None


def is_connected(ssid: str, interface: str | None = None) -> bool:
    return current_ssid(interface) == ssid


def wait_for_ssid(ssid: str, interface: str | None = None,
                  timeout: float = TIMEOUT_S) -> bool:
    """Poll a rescan until the camera's AP appears.

    The AP is woken over BLE and takes a moment to start beaconing, so trying
    to connect before it is visible only burns the timeout.
    """
    _require_nmcli()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        args = ["device", "wifi", "list", "--rescan", "yes"]
        if interface:
            args += ["ifname", interface]
        res = _nmcli("-t", "-f", "SSID", *args, timeout=timeout)
        if any(line.strip() == ssid for line in res.stdout.splitlines()):
            return True
        time.sleep(1.0)
    return False


def join(ssid: str, password: str, interface: str | None = None,
         timeout: float = TIMEOUT_S) -> None:
    """Create or update the camera profile and connect to it."""
    _require_nmcli()

    if not wait_for_ssid(ssid, interface, timeout=timeout):
        raise TimeoutError(
            f"{ssid} never appeared in a scan within {timeout}s -- the camera's "
            "access point is asleep. It is woken over BLE (0x53/0x10), so run "
            "without --skip-ble."
        )

    # Delete then recreate rather than modify: the SSID changes with the camera,
    # and a stale profile that still names the previous one connects to nothing
    # while reporting success.
    _nmcli("connection", "delete", PROFILE)

    add = [
        "connection", "add", "type", "wifi", "con-name", PROFILE,
        "ssid", ssid,
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password,
        # The camera AP has no internet. Without this NetworkManager makes it
        # the default route and everything else on the machine goes dark --
        # including the SSH session being used to run the rig.
        "ipv4.never-default", "yes",
        "ipv6.never-default", "yes",
        # Do not let it come up on its own after a reboot and steal the radio.
        "connection.autoconnect", "no",
    ]
    if interface:
        add += ["ifname", interface]
    res = _nmcli(*add)
    if res.returncode != 0:
        raise RuntimeError(f"nmcli add failed: {res.stdout}{res.stderr}")

    up = ["connection", "up", PROFILE]
    if interface:
        up += ["ifname", interface]
    res = _nmcli(*up, timeout=timeout + 10)
    if res.returncode != 0:
        raise RuntimeError(f"nmcli up failed: {res.stdout}{res.stderr}")


def reconnect(ssid: str, interface: str | None = None,
              timeout: float = TIMEOUT_S) -> bool:
    """Bring the existing profile back up. False if it will not come."""
    _require_nmcli()
    if is_connected(ssid, interface):
        return True
    res = _nmcli("connection", "up", PROFILE, timeout=timeout + 10)
    if res.returncode != 0:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_connected(ssid, interface):
            return True
        time.sleep(0.5)
    return False


def forget(ssid: str) -> None:
    """Remove the profile. `ssid` is accepted for symmetry with wifi_win --
    there is one profile per rig, not one per network."""
    if available():
        _nmcli("connection", "delete", PROFILE)
