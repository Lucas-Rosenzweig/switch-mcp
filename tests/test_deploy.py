"""The deployment files: placeholders, capabilities, and the root helper's safety."""
from pathlib import Path

DEPLOY = Path(__file__).parents[1] / "deploy"


def test_unit_has_the_capabilities_and_is_not_started_at_boot():
    unit = (DEPLOY / "switch-mcp.service.in").read_text()
    for cap in ("CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_NET_BIND_SERVICE"):
        assert cap in unit.split("AmbientCapabilities=")[1].splitlines()[0]
    assert "[Install]" not in unit
    assert "ExecStartPre=+@HELPER@ on" in unit
    assert "ExecStopPost=+@HELPER@ off" in unit
    assert "User=@USER@" in unit


def test_placeholders_are_the_ones_install_sh_fills():
    install = (DEPLOY.parent / "install.sh").read_text()
    for name in ("switch-mcp.service.in", "50-switch-mcp.rules.in"):
        text = (DEPLOY / name).read_text()
        for placeholder in {part.split("@")[0] for part in text.split("@")[1::2]}:
            assert f"@{placeholder}@" in install


def test_polkit_rule_is_limited_to_one_unit_and_user():
    rule = (DEPLOY / "50-switch-mcp.rules.in").read_text()
    assert 'action.lookup("unit") == "switch-mcp.service"' in rule
    assert 'subject.user == "@USER@"' in rule
    assert "enable" not in rule


def test_restoring_bluetooth_does_not_wait_for_bluetoothd():
    # ExecStopPost runs while bluetooth.service's stop waits for this unit.
    helper = (DEPLOY / "bluetooth-mode").read_text()
    off = helper.split("\noff)")[1].split(";;")[0]
    assert "restart_bluetoothd --no-block" in off
