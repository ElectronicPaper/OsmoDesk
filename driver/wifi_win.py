"""Join the camera SoftAP on Windows via netsh.

The camera's access point has no internet. Whichever adapter joins it loses
its normal connection for the duration, so use a second Wi-Fi adapter (a cheap
USB dongle) if you want to stay online while developing.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

PROFILE_TEMPLATE = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{password}</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
</WLANProfile>
"""


def _netsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["netsh", *args], capture_output=True, text=True, check=False
    )


def list_interfaces() -> list[str]:
    out = _netsh("wlan", "show", "interfaces").stdout
    names = []
    for line in out.splitlines():
        if line.strip().lower().startswith("name"):
            names.append(line.split(":", 1)[1].strip())
    return names


def join(ssid: str, password: str, interface: str | None = None,
         timeout: float = 25.0) -> None:
    """Add a WPA2 profile for the camera AP and connect to it."""
    xml = PROFILE_TEMPLATE.format(ssid=escape(ssid), password=escape(password))
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "osmo.xml"
        path.write_text(xml, encoding="utf-8")
        add = ["wlan", "add", "profile", f"filename={path}", "user=current"]
        if interface:
            add.append(f"interface={interface}")
        res = _netsh(*add)
        if res.returncode != 0:
            raise RuntimeError(f"netsh add profile failed: {res.stdout}{res.stderr}")

    # The AP is woken over BLE and takes a moment to start beaconing. Connecting
    # before it is visible just burns the timeout, so wait for it in the scan.
    if not wait_for_ssid(ssid, interface, timeout=timeout):
        raise TimeoutError(
            f"{ssid} never appeared in a scan within {timeout}s -- the camera's "
            "access point is asleep. It is woken over BLE (0x53/0x10), so run "
            "without --skip-ble."
        )

    log.info("joining %s ...", ssid)
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        connect = ["wlan", "connect", f"name={ssid}", f"ssid={ssid}"]
        if interface:
            connect.append(f"interface={interface}")
        res = _netsh(*connect)
        if res.returncode != 0 and attempt == 1:
            log.debug("netsh connect: %s%s", res.stdout.strip(), res.stderr.strip())

        for _ in range(6):
            if is_connected(ssid, interface):
                log.info("associated to %s", ssid)
                time.sleep(2.0)  # let DHCP settle on 192.168.2.x
                return
            time.sleep(1.0)
        log.info("still not associated, retrying (attempt %d) ...", attempt + 1)

    raise TimeoutError(f"did not associate to {ssid} within {timeout}s")


def wait_for_ssid(ssid: str, interface: str | None = None,
                  timeout: float = 25.0) -> bool:
    """Poll scan results until the network is being advertised."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        cmd = ["wlan", "show", "networks"]
        if interface:
            cmd.append(f"interface={interface}")
        if ssid in _netsh(*cmd).stdout:
            return True
        if not announced:
            log.info("waiting for %s to start beaconing ...", ssid)
            announced = True
        time.sleep(2.0)
    return False


def current_ssid(interface: str | None = None) -> str | None:
    """SSID the adapter is on right now, so it can be put back afterwards."""
    out = _netsh("wlan", "show", "interfaces").stdout
    seen_if = None
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("name"):
            seen_if = s.split(":", 1)[1].strip()
        if s.startswith("SSID") and not s.startswith("BSSID"):
            if interface is None or seen_if == interface:
                got = s.split(":", 1)[1].strip()
                return got or None
    return None


def reconnect(ssid: str, interface: str | None = None, timeout: float = 25.0) -> bool:
    """Rejoin a network that already has a saved profile. Best effort."""
    cmd = ["wlan", "connect", f"name={ssid}", f"ssid={ssid}"]
    if interface:
        cmd.append(f"interface={interface}")
    if _netsh(*cmd).returncode != 0:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_connected(ssid, interface):
            log.info("reconnected to %s", ssid)
            return True
        time.sleep(1.0)
    return False


def is_connected(ssid: str, interface: str | None = None) -> bool:
    out = _netsh("wlan", "show", "interfaces").stdout
    current_if = None
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("name"):
            current_if = s.split(":", 1)[1].strip()
        if s.startswith("SSID") and not s.startswith("BSSID"):
            got = s.split(":", 1)[1].strip()
            if got == ssid and (interface is None or current_if == interface):
                return True
    return False


def forget(ssid: str) -> None:
    _netsh("wlan", "delete", "profile", f"name={ssid}")
