"""Corrections applied to NUXBT before it forks its worker processes.

NUXBT is used as shipped; what the Switch 2 needs differently is patched in
here, in one place, so the workers inherit it.
"""
from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Iterable

from .adapter_class import GAMEPAD_CLASS, format_class, is_gamepad, read_class

log = logging.getLogger(__name__)

_SWITCH_ALIAS = "NINTENDO SWITCH"
_HANGUPS_BEFORE_REMOVAL = 2
_HANGUP_POLL_S = 0.1
# The Switch 2 sometimes hangs up on a controller it knows and accepts it a
# few seconds later (seen on the console: refused, then accepted on the next
# attempt with the same key and class). NUXBT gives up after one refusal and
# waits for a new pairing, which needs someone at the console.
_RECONNECT_ATTEMPTS = 4
_RECONNECT_RETRY_S = 3.0
# Asked of the kernel for the report stream; it rounds up to its minimum,
# which holds a handful of reports.
_SEND_BUFFER = 1
_QUEUE_LOG_S = 60.0
_DROPPED_WARNING = 0.1
# Subcommands arrive in bursts when the link comes up and rarely afterwards.
# The cap keeps a console that repeats one of them from flooding the log.
_LOGGED_PER_SUBCOMMAND = 20
_SUBCOMMAND_REPORT = 0x01
_SUBCOMMAND_NAMES = {
    0x01: "manual pairing",
    0x02: "device info",
    0x03: "set input report mode",
    0x04: "trigger buttons elapsed time",
    0x06: "set HCI state",
    0x07: "reset pairing info",
    0x08: "set shipment low power state",
    0x10: "SPI flash read",
    0x11: "SPI flash write",
    0x12: "SPI sector erase",
    0x20: "reset MCU",
    0x21: "set MCU config",
    0x22: "set MCU state",
    0x30: "set player lights",
    0x38: "set HOME light",
    0x40: "enable IMU",
    0x41: "set IMU sensitivity",
    0x48: "enable vibration",
    0x50: "get regulated voltage",
}


class ReportSocket:
    """The interrupt channel, with a send queue too short to fall behind.

    Whenever the radio is busy elsewhere (the PC trying to reach another
    device, for one) the reports pile up in the kernel. Measured on the
    console: the link fell from 132 to 16 reports a second,
    the default buffer filled with several seconds of stale states, and from
    then on new reports were dropped, short presses with them. Every report
    carries the whole state, so only the latest matters: a queue of a few
    reports loses nothing and keeps the console a few reports behind at most.
    """

    def __init__(self, sock) -> None:
        self._sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SEND_BUFFER)
        self._sent = self._dropped = 0
        self._next_log = time.monotonic() + _QUEUE_LOG_S

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def fileno(self) -> int:
        return self._sock.fileno()

    def sendall(self, data) -> None:
        try:
            self._sock.sendall(data)
            self._sent += 1
        except BlockingIOError:
            self._dropped += 1
            raise
        finally:
            now = time.monotonic()
            if now >= self._next_log:
                # A healthy link drops next to nothing. On the test machine
                # a script reconnecting another gamepad kept the radio busy
                # 40 seconds a minute and seven reports in eight were dropped.
                total = self._sent + self._dropped
                if total and self._dropped / total > _DROPPED_WARNING:
                    log.warning(
                        "%d of %d reports could not be sent in the last %.0f s: the radio is busy with something "
                        "else (another Bluetooth device being connected or searched for?), inputs will be late",
                        self._dropped, total, _QUEUE_LOG_S,
                    )
                else:
                    log.info("Reports in the last %.0f s: %d sent, %d dropped", _QUEUE_LOG_S, self._sent, self._dropped)
                self._sent = self._dropped = 0
                self._next_log = now + _QUEUE_LOG_S


def describe_command(data: bytes | None, handled: Iterable[int]) -> tuple[int, str] | None:
    """(subcommand id, log line) for an output report carrying a subcommand."""
    if not data or len(data) < 17 or data[0] != 0xA2 or data[1] != _SUBCOMMAND_REPORT:
        return None
    subcommand = data[11]
    text = f"0x{subcommand:02x} {_SUBCOMMAND_NAMES.get(subcommand, 'unknown')}"
    if subcommand in (0x10, 0x11, 0x12):
        text += f" at 0x{int.from_bytes(data[12:16], 'little'):04x}, {data[16]} bytes"
    else:
        text += f", arguments {bytes(data[12:17]).hex()}"
    if subcommand not in handled:
        text += " (NUXBT does not answer it)"
    return subcommand, text


def watch_hangups(
    running: Callable[[], bool],
    connected_switches: Callable[[], Iterable[str]],
    remove: Callable[[str], None],
    interval_s: float = _HANGUP_POLL_S,
) -> None:
    """While waiting for a pairing, drop the BlueZ record of a console that
    connects and hangs up twice: it holds a pairing the console rejects, and
    removing it lets the console pair afresh.

    NUXBT's own version removed any device that connected twice, whatever it
    was, and died with a traceback as soon as BlueZ deleted a record while it
    was being read.
    """
    connected: set[str] = set()
    hangups: dict[str, int] = {}
    while running():
        try:
            now = set(connected_switches())
        except Exception:
            # A record vanished between being listed and being read.
            log.debug("Could not list connected consoles", exc_info=True)
            time.sleep(interval_s)
            continue
        for path in connected - now:
            hangups[path] = hangups.get(path, 0) + 1
            if hangups[path] < _HANGUPS_BEFORE_REMOVAL:
                continue
            hangups[path] = 0
            try:
                remove(path)
                log.info("The console hung up twice; removed its Bluetooth record %s so it can pair afresh", path)
            except Exception as exc:
                log.info("Bluetooth record %s was already gone (%s)", path, exc)
        connected = now
        time.sleep(interval_s)


def _connected_switches(bluez) -> list[str]:
    import dbus
    from nuxbt.bluez import DEVICE_INTERFACE, SERVICE_NAME, find_objects

    paths = []
    for path in find_objects(bluez.bus, SERVICE_NAME, DEVICE_INTERFACE):
        try:
            props = dbus.Interface(
                bluez.bus.get_object(SERVICE_NAME, path), "org.freedesktop.DBus.Properties"
            )
            if props.Get(DEVICE_INTERFACE, "Connected") and (
                str(props.Get(DEVICE_INTERFACE, "Alias")).upper() == _SWITCH_ALIAS
            ):
                paths.append(path)
        except dbus.exceptions.DBusException:
            continue
    return paths


def _adapter_index(bluez) -> int:
    return int(bluez.device_id.replace("hci", ""))


def apply() -> None:
    """Patch NUXBT's classes. Idempotent; call before nuxbt.Nuxbt() forks."""
    from nuxbt.bluez import BlueZ
    from nuxbt.controller.protocol import ControllerProtocol, SwitchReportParser
    from nuxbt.controller.server import ControllerServer

    if getattr(ControllerServer, "_switch_mcp_patched", False):
        return
    ControllerServer._switch_mcp_patched = True

    # NUXBT sends a raw HCI Reset when a controller process exits. The
    # kernel and bluetoothd are not told, so the adapter loses the setup
    # they gave it and the next pairing fails until bluetoothd restarts.
    BlueZ.reset_adapter = lambda self: None

    # NUXBT only announces itself as a gamepad when pairing. The Switch 2
    # hangs up on a reconnecting device of any other class, so announce it
    # before reconnecting too, record the class the attempt went out with,
    # and try again before falling back to pairing.
    original_reconnect = ControllerServer.reconnect

    def reconnect(server, reconnect_address):
        index = _adapter_index(server.bt)
        for attempt in range(1, _RECONNECT_ATTEMPTS + 1):
            server.bt.set_class(f"0x{GAMEPAD_CLASS:06x}")
            time.sleep(0.1)
            before = read_class(index)
            if not is_gamepad(before):
                log.warning("The adapter class is %s, not a gamepad; the console will refuse it", format_class(before))
            try:
                sockets = original_reconnect(server, reconnect_address)
            except OSError as exc:
                log.info(
                    "Reconnection to %s refused, attempt %d of %d (%s); adapter class was %s before and %s after",
                    reconnect_address, attempt, _RECONNECT_ATTEMPTS, exc,
                    format_class(before), format_class(read_class(index)),
                )
                if attempt == _RECONNECT_ATTEMPTS:
                    raise
                time.sleep(_RECONNECT_RETRY_S)
            else:
                log.info("Reconnected to %s with adapter class %s", reconnect_address, format_class(before))
                return ReportSocket(sockets[0]), sockets[1]

    ControllerServer.reconnect = reconnect

    original_connect = ControllerServer.connect

    def connect(server):
        itr, ctrl = original_connect(server)
        return ReportSocket(itr), ctrl

    ControllerServer.connect = connect

    def connection_reset_watchdog(server):
        watch_hangups(
            lambda: server._crw_running,
            lambda: _connected_switches(server.bt),
            server.bt.remove_device,
        )

    ControllerServer.connection_reset_watchdog = connection_reset_watchdog

    # What the console asks of the controller is the only clue to why it
    # later refuses it, so every subcommand is logged, not only in debug mode.
    original_process = ControllerProtocol.process_commands
    handled = frozenset(SwitchReportParser.SUBCOMMANDS)

    def process_commands(protocol, data):
        described = describe_command(data, handled)
        if described is not None:
            subcommand, text = described
            counts = protocol.__dict__.setdefault("_switch_mcp_logged", {})
            counts[subcommand] = counts.get(subcommand, 0) + 1
            if counts[subcommand] <= _LOGGED_PER_SUBCOMMAND:
                log.info("Console subcommand %s", text)
        return original_process(protocol, data)

    ControllerProtocol.process_commands = process_commands
