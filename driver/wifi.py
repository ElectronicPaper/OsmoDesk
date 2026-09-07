"""Pick the Wi-Fi backend for whatever this is running on.

The rig was written on Windows and belongs on a Raspberry Pi: a laptop on a
stand is a thing to trip over, and the Pi can live on the tripod. Only two
pieces of the driver were ever Windows-specific, and joining the camera's
access point was the larger one.

Importing this instead of `wifi_win` directly means the rest of the code never
asks what it is running on. The backends present the same names and the same
behaviour; where they cannot, the difference is documented in the backend
rather than papered over here.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from . import wifi_win as backend
else:
    from . import wifi_nix as backend

NAME = backend.__name__.rsplit(".", 1)[-1]

join = backend.join
list_interfaces = backend.list_interfaces
current_ssid = backend.current_ssid
is_connected = backend.is_connected
wait_for_ssid = backend.wait_for_ssid
reconnect = backend.reconnect
forget = backend.forget


def available() -> bool:
    """True if this machine can actually be told to join a network.

    `wifi_win` has no such check because netsh ships with Windows. nmcli does
    not ship with every Linux, so the Pi build can be honest about it at
    startup rather than failing halfway through a connection.
    """
    checker = getattr(backend, "available", None)
    return True if checker is None else checker()
