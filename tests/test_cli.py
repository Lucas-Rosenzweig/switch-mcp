import pytest

from switch_mcp import cli, server


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SWITCH_MCP_CONFIG", str(tmp_path / "config.toml"))
    for name in ("PORT", "HOST"):
        monkeypatch.delenv(f"SWITCH_MCP_{name}", raising=False)


def test_serve_forwards_its_options(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "main", seen.append)
    assert cli.main(["serve", "--transport", "stdio"]) == 0
    # The former invocation without a subcommand still serves.
    assert cli.main(["--transport", "streamable-http"]) == 0
    assert seen == [["--transport", "stdio"], ["--transport", "streamable-http"]]


def test_config_url(capsys, monkeypatch):
    monkeypatch.setenv("SWITCH_MCP_PORT", "9001")
    assert cli.main(["config", "--url"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:9001/mcp"


def test_configuration_error_is_one_line(capsys, monkeypatch):
    monkeypatch.setenv("SWITCH_MCP_PORT", "nope")
    assert cli.main(["config"]) == 2
    assert capsys.readouterr().err.startswith("Configuration error: port")


def test_bt_status_reads_the_override(capsys, monkeypatch, tmp_path):
    override = tmp_path / "switch-mcp.conf"
    monkeypatch.setattr(cli, "BT_OVERRIDE", override)
    cli.main(["bt", "status"])
    override.write_text("[Service]\n")
    cli.main(["bt", "status"])
    assert capsys.readouterr().out.split() == ["off", "on"]


def test_bt_off_refuses_while_the_service_runs(monkeypatch):
    monkeypatch.setattr(cli, "service_active", lambda: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("must not run the helper"))
    assert cli.main(["bt", "off"]) == 1


def test_start_without_install(capsys, monkeypatch):
    monkeypatch.setattr(cli, "service_installed", lambda: False)
    assert cli.main(["start"]) == 1
    assert "install.sh" in capsys.readouterr().err


def test_start_waits_for_the_port(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "service_installed", lambda: True)
    monkeypatch.setattr(cli, "service_active", lambda: True)
    monkeypatch.setattr(cli, "_systemctl", lambda *args: calls.append(args) or cli.subprocess.CompletedProcess(args, 0, "", ""))
    answers = iter([False, False, True])
    monkeypatch.setattr(cli, "port_open", lambda host, port: next(answers))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["start"]) == 0
    assert calls == [("start", "switch-mcp.service")]
    assert "http://127.0.0.1:8765/mcp" in capsys.readouterr().out


def test_start_reports_a_service_that_died(capsys, monkeypatch):
    monkeypatch.setattr(cli, "service_installed", lambda: True)
    monkeypatch.setattr(cli, "service_active", lambda: False)
    monkeypatch.setattr(cli, "_systemctl", lambda *args: cli.subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(cli, "port_open", lambda host, port: False)
    assert cli.main(["start"]) == 1
    assert "journalctl" in capsys.readouterr().err
