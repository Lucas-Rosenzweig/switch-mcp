import logging

from switch_mcp.controller import nuxbt_patches
from switch_mcp.controller.nuxbt_patches import describe_command, watch_hangups

HANDLED = {0x02, 0x10, 0x30}


def report(subcommand, arguments=b"", report_id=0x01):
    body = bytes([0xA2, report_id, 0x00]) + bytes(8) + bytes([subcommand]) + arguments
    return body + bytes(50 - len(body))


def test_spi_read_shows_address_and_length():
    assert describe_command(report(0x10, bytes.fromhex("0020000018")), HANDLED) == (
        0x10, "0x10 SPI flash read at 0x2000, 24 bytes",
    )


def test_unanswered_subcommands_are_flagged():
    assert describe_command(report(0x07), HANDLED) == (
        0x07, "0x07 reset pairing info, arguments 0000000000 (NUXBT does not answer it)",
    )
    assert describe_command(report(0x99, b"\x01"), HANDLED)[1].startswith("0x99 unknown, arguments 01")
    assert "does not answer" not in describe_command(report(0x30, b"\x01"), HANDLED)[1]


def test_rumble_and_empty_reports_are_not_logged():
    assert describe_command(None, HANDLED) is None
    assert describe_command(b"", HANDLED) is None
    assert describe_command(report(0x00, report_id=0x10), HANDLED) is None
    assert describe_command(b"\xa1" + report(0x02)[1:], HANDLED) is None


def run_watch(snapshots, remove):
    """Play one listing per poll; a listing that is an exception is raised."""
    steps = iter(snapshots)
    state = {"left": len(snapshots)}

    def running():
        return state["left"] > 0

    def connected():
        state["left"] -= 1
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        return step

    watch_hangups(running, connected, remove, interval_s=0)


def test_record_is_removed_after_two_hangups():
    removed = []
    run_watch([["sw"], [], ["sw"], [], ["sw"], []], removed.append)
    assert removed == ["sw"]


def test_a_single_hangup_removes_nothing():
    removed = []
    run_watch([["sw"], [], ["sw"], ["sw"]], removed.append)
    assert removed == []


def test_dbus_errors_do_not_stop_the_watch(caplog):
    def remove(path):
        raise RuntimeError("org.freedesktop.DBus.Error.UnknownObject")

    with caplog.at_level(logging.INFO, logger=nuxbt_patches.__name__):
        # The listing fails once, then the record is already gone at removal.
        run_watch([["sw"], RuntimeError("UnknownObject"), [], ["sw"], []], remove)
    assert "already gone" in caplog.text


def test_patches_replace_nuxbt_behaviour_once():
    import pytest

    pytest.importorskip("nuxbt.nuxbt")
    from nuxbt.bluez import BlueZ
    from nuxbt.controller.protocol import ControllerProtocol
    from nuxbt.controller.server import ControllerServer

    nuxbt_patches.apply()
    patched = (ControllerServer.reconnect, ControllerServer.connection_reset_watchdog,
               ControllerProtocol.process_commands, BlueZ.reset_adapter)
    nuxbt_patches.apply()
    assert patched == (ControllerServer.reconnect, ControllerServer.connection_reset_watchdog,
                       ControllerProtocol.process_commands, BlueZ.reset_adapter)
    assert all(function.__module__ == nuxbt_patches.__name__ for function in patched)


def test_patched_protocol_still_answers_and_logs(caplog):
    import pytest

    pytest.importorskip("nuxbt.nuxbt")
    from nuxbt.controller.controller import ControllerTypes
    from nuxbt.controller.protocol import ControllerProtocol

    nuxbt_patches.apply()
    protocol = ControllerProtocol(ControllerTypes.PRO_CONTROLLER, "AA:BB:CC:DD:EE:FF")
    with caplog.at_level(logging.INFO, logger=nuxbt_patches.__name__):
        for _ in range(nuxbt_patches._LOGGED_PER_SUBCOMMAND + 5):
            protocol.process_commands(report(0x02))
            reply = protocol.get_report()
        protocol.process_commands(None)
    assert reply[1] == 0x21 and reply[15] == 0x02  # device info still answered
    assert caplog.text.count("Console subcommand 0x02 device info") == nuxbt_patches._LOGGED_PER_SUBCOMMAND


class FakeSocket:
    def __init__(self, full=False):
        self.full, self.sent, self.options = full, [], []

    def setsockopt(self, *option):
        self.options.append(option)

    def sendall(self, data):
        if self.full:
            raise BlockingIOError
        self.sent.append(data)

    def recv(self, size):
        return b"reply"


def test_report_socket_keeps_a_short_queue_and_counts_drops(caplog, monkeypatch):
    import socket

    inner = FakeSocket()
    reports = nuxbt_patches.ReportSocket(inner)
    assert inner.options == [(socket.SOL_SOCKET, socket.SO_SNDBUF, nuxbt_patches._SEND_BUFFER)]
    reports.sendall(b"a")
    assert inner.sent == [b"a"] and reports.recv(50) == b"reply"

    inner.full = True
    monkeypatch.setattr(nuxbt_patches, "_QUEUE_LOG_S", 0)
    reports._next_log = 0
    with caplog.at_level(logging.INFO, logger=nuxbt_patches.__name__):
        try:
            reports.sendall(b"b")
        except BlockingIOError:
            pass  # NUXBT skips the cycle on this error
        else:
            raise AssertionError("a full queue must still be reported to NUXBT")
    assert "1 of 2 reports could not be sent" in caplog.text


def test_a_refused_reconnection_is_tried_again(monkeypatch):
    import pytest

    pytest.importorskip("nuxbt.nuxbt")
    from nuxbt.controller.server import ControllerServer

    nuxbt_patches.apply()
    monkeypatch.setattr(nuxbt_patches, "read_class", lambda index=0: 0x002508)
    monkeypatch.setattr(nuxbt_patches.time, "sleep", lambda seconds: None)

    class Adapter:
        device_id = "hci0"
        classes = []

        def set_class(self, device_class):
            self.classes.append(device_class)

    class Server:
        bt = Adapter()
        # Stands for the two sockets.connect() NUXBT performs.
        outcomes = [OSError("refused"), OSError("refused"), (FakeSocket(), "ctrl")]

    def original(server, address):
        outcome = server.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # The patch closed over NUXBT's own reconnect; reach it through its cell.
    cell = next(c for c in ControllerServer.reconnect.__closure__ if callable(c.cell_contents)
                and c.cell_contents.__name__ == "reconnect")
    real = cell.cell_contents
    cell.cell_contents = original
    try:
        itr, ctrl = ControllerServer.reconnect(Server(), "AA:BB:CC:DD:EE:FF")
        assert isinstance(itr, nuxbt_patches.ReportSocket) and ctrl == "ctrl"
        assert Adapter.classes == ["0x002508"] * 3  # announced again before each attempt

        Server.outcomes = [OSError("refused")] * nuxbt_patches._RECONNECT_ATTEMPTS
        with pytest.raises(OSError):
            ControllerServer.reconnect(Server(), "AA:BB:CC:DD:EE:FF")
        assert Server.outcomes == []
    finally:
        cell.cell_contents = real
