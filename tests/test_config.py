import pytest

from switch_mcp.config import Config, ConfigError, config_path


def load(tmp_path, text=None, **env):
    path = tmp_path / "config.toml"
    if text is not None:
        path.write_text(text)
    return Config.load(path=path, environ={f"SWITCH_MCP_{k.upper()}": v for k, v in env.items()})


def test_defaults_without_file(tmp_path):
    assert load(tmp_path) == Config()
    assert Config().url == "http://127.0.0.1:8765/mcp"


def test_file_then_environment(tmp_path):
    config = load(tmp_path, 'port = 9000\nvideo_device = "/dev/video2"\nsettle_s = 1.5\n', port="9100")
    assert config.port == 9100
    assert config.video_device == "/dev/video2"
    assert config.settle_s == 1.5


def test_environment_values_are_converted(tmp_path):
    config = load(tmp_path, capture_idle_s="12", max_hold_s="40", screenshot_width="720")
    assert (config.capture_idle_s, config.max_hold_s, config.screenshot_width) == (12.0, 40.0, 720)


def test_switch_address_is_optional_and_normalised(tmp_path):
    assert load(tmp_path).switch_address is None
    assert load(tmp_path, switch_address="").switch_address is None
    assert load(tmp_path, 'switch_address = "98:e2:55:99:66:b9"').switch_address == "98:E2:55:99:66:B9"


@pytest.mark.parametrize(
    "text, env",
    [
        ("colour = 1\n", {}),
        ("port = 'x'\n", {}),
        ("port = [\n", {}),
        ("", {"port": "8765.5"}),
        ("", {"port": "70000"}),
        ("", {"backend": "joycontrol"}),
        ("", {"switch_address": "98:E2:55"}),
        ("", {"default_hold_s": "60"}),
    ],
)
def test_invalid_settings_are_reported(tmp_path, text, env):
    with pytest.raises(ConfigError):
        load(tmp_path, text, **env)


def test_config_path(monkeypatch, tmp_path):
    monkeypatch.delenv("SWITCH_MCP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "switch-mcp" / "config.toml"
    monkeypatch.setenv("SWITCH_MCP_CONFIG", str(tmp_path / "other.toml"))
    assert config_path() == tmp_path / "other.toml"


def test_example_file_is_valid_and_matches_defaults(tmp_path):
    from pathlib import Path

    example = Path(__file__).parents[1] / "deploy" / "config.example.toml"
    assert Config.load(path=example, environ={}) == Config()
    # Every setting is documented there.
    text = example.read_text()
    for name in Config.__dataclass_fields__:
        assert f"# {name} = " in text
