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
