"""Runtime configuration.

Each setting comes, by increasing priority, from its default, from
~/.config/switch-mcp/config.toml, then from a SWITCH_MCP_<NAME> environment
variable. SWITCH_MCP_CONFIG points to another file.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ENV_PREFIX = "SWITCH_MCP_"
_ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    if explicit := os.environ.get("SWITCH_MCP_CONFIG"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "switch-mcp" / "config.toml"


@dataclass(frozen=True)
class Config:
    # "nuxbt" drives a real Switch over Bluetooth; "fake" records inputs only.
    backend: str = "nuxbt"
    video_device: str = "/dev/video0"
    # Seconds without a screenshot before the capture device is released.
    capture_idle_s: float = 30.0
    # Width in pixels of a default screenshot.
    screenshot_width: int = 1280
    # A held input is released automatically after this long without renewal.
    default_hold_s: float = 5.0
    max_hold_s: float = 30.0
    # Seconds after the link comes up during which inputs are still refused.
    settle_s: float = 3.0
    # Console to reconnect to. Optional: learnt at pairing when left empty.
    switch_address: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        if self.backend not in ("nuxbt", "fake"):
            raise ConfigError(f"backend must be 'nuxbt' or 'fake', not {self.backend!r}")
        if self.switch_address is not None:
            address = self.switch_address.strip().upper()
            if not _ADDRESS.match(address):
                raise ConfigError(f"switch_address {self.switch_address!r} is not a Bluetooth address (AA:BB:CC:DD:EE:FF)")
            object.__setattr__(self, "switch_address", address)
        if not 0 < self.port < 65536:
            raise ConfigError(f"port {self.port} is out of range")
        for name in ("capture_idle_s", "default_hold_s", "max_hold_s", "screenshot_width"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")
        if self.settle_s < 0:
            raise ConfigError("settle_s cannot be negative")
        if self.default_hold_s > self.max_hold_s:
            raise ConfigError("default_hold_s cannot exceed max_hold_s")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @classmethod
    def load(cls, path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
        """Defaults, overridden by the TOML file if it exists, then by the environment."""
        path = config_path() if path is None else path
        environ = os.environ if environ is None else environ
        values: dict[str, object] = {}
        try:
            with open(path, "rb") as file:
                values.update(tomllib.load(file))
        except FileNotFoundError:
            pass
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        known = {field.name: field for field in fields(cls)}
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise ConfigError(f"{path}: unknown setting(s) {', '.join(unknown)}; expected {', '.join(known)}")
        for name in known:
            raw = environ.get(ENV_PREFIX + name.upper())
            if raw is not None:
                values[name] = raw
        try:
            return cls(**{name: _coerce(known[name].type, name, value) for name, value in values.items()})
        except TypeError as exc:
            raise ConfigError(str(exc)) from exc


def _coerce(annotation: str, name: str, value: object) -> object:
    """Convert a TOML or environment value to the field's type."""
    try:
        if annotation == "float":
            return float(value)  # type: ignore[arg-type]
        if annotation == "int":
            if isinstance(value, float) or (isinstance(value, str) and not value.strip().lstrip("-").isdigit()):
                raise ValueError
            return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: {value!r} is not a valid {annotation}") from None
    if annotation == "str | None":
        return str(value) if value not in (None, "") else None
    return str(value)
