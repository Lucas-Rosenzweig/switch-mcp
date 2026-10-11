from switch_mcp.controller.linkmode import parse_mode_change


def test_mode_changes_are_described():
    # event packet, Mode Change, length 6, status, handle, mode, interval in slots
    assert parse_mode_change(bytes.fromhex("04140600000102" "1800")) == "sniff, one transmit slot every 15 ms"
    assert parse_mode_change(bytes.fromhex("04140600000100" "0000")) == "active"
    assert parse_mode_change(bytes.fromhex("0414060c000102" "1800")) == "mode change failed (status 12)"
    assert parse_mode_change(bytes.fromhex("040e0701230c0004016c")) is None
    assert parse_mode_change(b"") is None
