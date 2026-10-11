"""Bluetooth Pro Controller emulation through NUXBT (BlueZ).

NUXBT needs two things from the system, both checked here so failures are
explained instead of surfacing as a socket error:
- bluetoothd running without its plugins (they claim the HID L2CAP ports);
- this process holding CAP_NET_RAW, CAP_NET_ADMIN and CAP_NET_BIND_SERVICE.
See deploy/ for the scripts that provide both.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from . import nuxbt_patches
from .adapter_class import claim_gamepad_class, set_kernel_class
from .base import PadState
from .linkkeys import LinkKeyKeeper

log = logging.getLogger(__name__)

# Linux capability bit numbers.
_REQUIRED_CAPS = {"CAP_NET_BIND_SERVICE": 10, "CAP_NET_ADMIN": 12, "CAP_NET_RAW": 13}
_BUTTON_KEYS = {"UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT"}
_STICK_KEYS = {"left": "L_STICK", "right": "R_STICK"}
_ADAPTER_IFACE = "org.bluez.Adapter1"
# NUXBT stops streaming reports when its input packet equals its idle packet.
# The Switch 2 copes badly with a stream that stops and starts: the release
# that ends a press is seen up to a second late, and the first press after a
# quiet period is delayed or swallowed. Measured on the console: with a
# continuous stream every short press moves a menu by exactly one row; with a
# stream that pauses, presses overshoot or vanish. A real controller never
# stops reporting, so neither do we: an extra key makes every packet
# "non-idle" to NUXBT, and the stream starts as soon as the link is up.
_KEEP_STREAMING = "SWITCH_MCP_KEEP_STREAMING"
# Inputs are refused for this long after the stream starts, while the console
# switches from its slow post-connection polling to the normal rate.
_SETTLE_S = 3.0
_WATCH_INTERVAL_S = 0.2


class SetupError(RuntimeError):
    """The system is not prepared for Bluetooth controller emulation."""


def missing_capabilities() -> list[str]:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("CapEff:"):
                effective = int(line.split()[1], 16)
                return [name for name, bit in _REQUIRED_CAPS.items() if not effective >> bit & 1]
    return list(_REQUIRED_CAPS)


def bluetoothd_plugins_disabled() -> bool:
    result = subprocess.run(
        ["systemctl", "show", "bluetooth.service", "--property=ExecStart"],
        capture_output=True, text=True, check=False,
    )
    return "--noplugin" in result.stdout


def check_setup() -> None:
    missing = missing_capabilities()
    if missing:
        raise SetupError(
            f"Missing capabilities {missing}. Start the server with deploy/run-with-caps "
            "or install deploy/switch-mcp.service."
        )
    if not bluetoothd_plugins_disabled():
        raise SetupError(
            "bluetoothd still has its plugins enabled, so the HID ports are taken. "
            "Run: deploy/bluetooth-session on"
        )


def build_packet(packet: dict, state: PadState) -> dict:
    """Fill a NUXBT input packet (from create_input_packet) with `state`."""
    for button in state.buttons:
        if button in _STICK_KEYS.values():
            packet[button]["PRESSED"] = True
        else:
            packet[_BUTTON_KEYS.get(button, button)] = True
    for side, key in _STICK_KEYS.items():
        x, y = getattr(state, side)
        packet[key]["X_VALUE"] = round(x * 100)
        packet[key]["Y_VALUE"] = round(y * 100)
    packet[_KEEP_STREAMING] = True
    return packet


class NuxbtBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nx = None
        self._index: int | None = None
        self._adapter_path: str | None = None
        self._original_alias: str | None = None
        self._original_class: tuple[int, int] | None = None
        self._setup_error: str | None = None
        self._keys = LinkKeyKeeper()
        self._streaming_since: float | None = None

    def start(self) -> None:
        """Start NUXBT's worker processes. Call before any thread exists:
        NUXBT forks, and forking a threaded process is unsafe."""
        with self._lock:
            self._ensure_started()

    def _ensure_started(self) -> None:
        if self._nx is not None:
            return
        try:
            check_setup()
            import nuxbt
        except (SetupError, ImportError) as exc:
            self._setup_error = str(exc)
            raise SetupError(self._setup_error) from exc
        self._setup_error = None
        # Patched before NUXBT forks its workers so they inherit it.
        nuxbt_patches.apply()
        # SWITCH_MCP_NUXBT_DEBUG=1 logs every message exchanged with the console.
        self._nx = nuxbt.Nuxbt(debug=os.environ.get("SWITCH_MCP_NUXBT_DEBUG") == "1")
        try:
            self._keys.start()
        except OSError:
            log.warning("Link keys will not be kept; every session will need a new pairing", exc_info=True)

    # -- adapter alias -----------------------------------------------------

    def _adapter_properties(self):
        import dbus

        bus = dbus.SystemBus()
        return dbus.Interface(
            bus.get_object("org.bluez", self._adapter_path), "org.freedesktop.DBus.Properties"
        )

    def _remember_alias(self) -> None:
        """NUXBT renames the adapter to 'Pro Controller' and never undoes it."""
        if self._original_alias is not None:
            return
        try:
            self._adapter_path = self._nx.get_available_adapters()[0]
            props = self._adapter_properties()
            alias = str(props.Get(_ADAPTER_IFACE, "Alias"))
            # A previous unclean exit may have left the controller name behind.
            self._original_alias = str(props.Get(_ADAPTER_IFACE, "Name")) if alias == "Pro Controller" else alias
        except Exception:
            log.warning("Could not read the adapter alias", exc_info=True)

    def _restore_alias(self) -> None:
        if self._original_alias is None:
            return
        try:
            self._adapter_properties().Set(_ADAPTER_IFACE, "Alias", self._original_alias)
        except Exception:
            log.warning("Could not restore the adapter alias", exc_info=True)

    # -- Backend -----------------------------------------------------------

    def _adapter_index(self) -> int:
        try:
            return int((self._adapter_path or "hci0").rsplit("hci", 1)[1])
        except ValueError:
            return 0

    def _claim_gamepad_class(self) -> None:
        """Keep the kernel from announcing the adapter as a computer while
        NUXBT announces it as a gamepad (see adapter_class)."""
        previous = claim_gamepad_class(self._adapter_index())
        if previous is not None and self._original_class is None:
            self._original_class = previous

    def _restore_class(self) -> None:
        if self._original_class is None:
            return
        try:
            set_kernel_class(*self._original_class, self._adapter_index())
        except OSError:
            log.warning("Could not restore the adapter class", exc_info=True)
        self._original_class = None

    def _load_known_consoles(self) -> list[str]:
        """Hand stored link keys to the kernel; returns the consoles they belong to."""
        try:
            return self._keys.load_into_kernel(self._adapter_index())
        except OSError:
            log.warning("Could not load stored link keys; a new pairing will be needed", exc_info=True)
            return []

    def connect(self) -> None:
        import nuxbt

        with self._lock:
            self._ensure_started()
            if self._index is not None:
                if self._controller_state().get("state") == "connected":
                    raise RuntimeError("Controller already connected")
                # Calling again while pairing restarts it, which reopens the
                # three-minute window during which the console can find us.
                self._remove_controller()
                time.sleep(0.5)
            self._remember_alias()
            self._claim_gamepad_class()
            # With a console whose link key we kept, NUXBT reconnects directly
            # and falls back to pairing (Change Grip/Order menu) if that fails.
            known = self._load_known_consoles()
            self._index = self._nx.create_controller(
                nuxbt.PRO_CONTROLLER, reconnect_address=known or None
            )
            self._streaming_since = None
            threading.Thread(
                target=self._watch_link, args=(self._index,), daemon=True, name="link-watch"
            ).start()

    def disconnect(self) -> None:
        with self._lock:
            if self._nx is not None and self._index is not None:
                self._remove_controller()

    def _controller_state(self) -> dict:
        try:
            return dict(self._nx.state[self._index])
        except Exception:
            return {"state": "crashed", "errors": "controller process is gone"}

    def _remove_controller(self) -> None:
        try:
            self._nx.remove_controller(self._index)
        except Exception:
            log.debug("remove_controller failed", exc_info=True)
        self._index = None

    def status(self) -> dict:
        with self._lock:
            if self._nx is None:
                return {"state": "idle", "backend": "nuxbt", "setup_error": self._setup_error}
            if self._index is None:
                return {"state": "idle", "backend": "nuxbt"}
            state = self._controller_state()
            name = state.get("state")
            if name == "connected" and (
                self._streaming_since is None or time.monotonic() - self._streaming_since < _SETTLE_S
            ):
                name = "settling"
            return {
                "state": name,
                "backend": "nuxbt",
                "known_consoles": self._keys.known_consoles(),
                "console": state.get("last_connection"),
                "error": state.get("errors") or None,
            }

    def apply(self, state: PadState) -> None:
        with self._lock:
            if self._nx is None or self._index is None:
                raise RuntimeError("Controller not created")
            self._send(state)

    def _send(self, state: PadState) -> None:
        # NUXBT re-sends this packet every cycle until it is replaced.
        self._nx.set_controller_input(self._index, build_packet(self._nx.create_input_packet(), state))

    def _watch_link(self, index: int) -> None:
        """Start the report stream each time this controller's link comes up
        (NUXBT also reconnects by itself after a drop)."""
        was_connected = False
        while True:
            time.sleep(_WATCH_INTERVAL_S)
            with self._lock:
                if self._nx is None or self._index != index:
                    return
                connected = self._controller_state().get("state") == "connected"
                if connected and not was_connected:
                    try:
                        self._send(PadState())
                        self._streaming_since = time.monotonic()
                    except Exception:
                        log.warning("Could not start the report stream", exc_info=True)
                        connected = False
                elif not connected:
                    self._streaming_since = None
                    if was_connected:
                        # NUXBT reconnects by itself and would replay the
                        # inputs held when the link dropped.
                        log.warning("Link to the console lost; inputs released")
                        try:
                            self._send(PadState())
                        except Exception:
                            log.warning("Could not clear the inputs after the link dropped", exc_info=True)
                was_connected = connected

    def close(self) -> None:
        with self._lock:
            if self._nx is None:
                return
            if self._index is not None:
                self._remove_controller()
            self._restore_alias()
            self._restore_class()
            self._nx._on_exit()
            self._nx = None
