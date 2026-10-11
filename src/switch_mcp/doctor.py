"""switch-mcp doctor: check every piece the server depends on and say what to do.

Read-only: it changes nothing. A check that cannot apply right now (the
service is stopped, so the HDMI signal cannot be read through it) says so
instead of failing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import cli
from .config import Config, ConfigError, config_path
from .controller.linkkeys import default_store_path

UNIT_PATH = Path("/etc/systemd/system") / cli.SERVICE
POLKIT_RULE = Path("/etc/polkit-1/rules.d/50-switch-mcp.rules")
REQUIRED_CAPS = ("CAP_NET_ADMIN", "CAP_NET_BIND_SERVICE", "CAP_NET_RAW")

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_MARKS = {OK: "✓", INFO: "·", WARN: "!", FAIL: "✗"}


@dataclass
class Result:
    level: str
    name: str
    message: str
    fix: str = ""


def check_config() -> tuple[Result, Config | None]:
    path = config_path()
    try:
        config = Config.load()
    except ConfigError as exc:
        return Result(FAIL, "configuration", str(exc), f"edit {path}"), None
    where = str(path) if path.exists() else f"defaults ({path} absent)"
    return Result(OK, "configuration", where), config


def check_adapter() -> Result:
    adapters = sorted(p.name for p in Path("/sys/class/bluetooth").glob("hci*")) if Path("/sys/class/bluetooth").exists() else []
    if not adapters:
        return Result(FAIL, "bluetooth adapter", "none found", "plug in or enable the adapter (rfkill list; bluetoothctl power on)")
    return Result(OK, "bluetooth adapter", ", ".join(adapters))


def check_bluetoothd() -> Result:
    active = subprocess.run(["systemctl", "is-active", "bluetooth.service"], capture_output=True, text=True, check=False)
    if active.stdout.strip() != "active":
        return Result(FAIL, "bluetoothd", active.stdout.strip() or "not running", "sudo systemctl start bluetooth")
    if cli.bt_mode_on():
        return Result(OK, "bluetoothd", "emulation mode (plugins off; headsets and gamepads unavailable)")
    return Result(OK, "bluetoothd", "normal mode; switch-mcp start switches it to emulation mode")


def check_nuxbt(config: Config | None) -> Result:
    if config is not None and config.backend == "fake":
        return Result(INFO, "nuxbt", "not needed with backend = fake")
    if importlib.util.find_spec("nuxbt") is None:
        return Result(FAIL, "nuxbt", "not installed", "uv sync --extra bluetooth  (or rerun ./install.sh)")
    return Result(OK, "nuxbt", "installed")


def check_service() -> list[Result]:
    results = []
    if not UNIT_PATH.exists():
        return [Result(FAIL, "service", f"{UNIT_PATH} missing", "run ./install.sh")]
    unit = UNIT_PATH.read_text()
    missing = [cap for cap in REQUIRED_CAPS if cap not in unit]
    if missing:
        results.append(Result(FAIL, "service", f"unit lacks {', '.join(missing)}", "rerun ./install.sh"))
    else:
        state = subprocess.run(["systemctl", "is-active", cli.SERVICE], capture_output=True, text=True, check=False)
        state = state.stdout.strip() or "unknown"
        if state == "failed":
            results.append(Result(WARN, "service", "installed, failed last time", "journalctl -u switch-mcp -e"))
        else:
            results.append(Result(OK, "service", f"installed, {state}"))
    if not os.access(POLKIT_RULE.parent, os.R_OK | os.X_OK):
        # 0750 root:polkitd on Arch: the rule cannot be seen from here.
        results.append(Result(INFO, "polkit rule", "not readable by this user; switch-mcp start asks for a password if it is missing"))
    elif not POLKIT_RULE.exists():
        results.append(Result(WARN, "polkit rule", f"{POLKIT_RULE} missing; start/stop will ask for a password", "rerun ./install.sh"))
    else:
        results.append(Result(OK, "polkit rule", "start/stop without password"))
    results.append(_check_helper())
    return results


def _check_helper() -> Result:
    helper = cli.BT_HELPER
    if not helper.exists():
        return Result(FAIL, "bluetooth helper", f"{helper} missing", "rerun ./install.sh")
    # The service runs it as root: anyone able to edit it would get root.
    for path in (helper, *helper.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return Result(FAIL, "bluetooth helper", f"{path} is writable by a non-root user", "rerun ./install.sh")
        if path == Path("/"):
            break
    return Result(OK, "bluetooth helper", str(helper))


def check_listening(config: Config) -> Result:
    if cli.port_open(config.host, config.port):
        return Result(OK, "mcp server", f"{config.url} answers")
    return Result(INFO, "mcp server", f"nothing on {config.host}:{config.port}", "switch-mcp start")


def check_capture_device(config: Config) -> Result:
    device = Path(config.video_device)
    if not device.exists():
        fix = "check the capture card and its driver"
        if not Path("/sys/module/sc0710").exists():
            fix = "the sc0710 module is not loaded: rebuild it for the current kernel (dkms autoinstall), then sudo modprobe sc0710"
        return Result(FAIL, "capture card", f"{device} does not exist", fix)
    if not os.access(device, os.R_OK | os.W_OK):
        return Result(FAIL, "capture card", f"no access to {device}", "add yourself to the video group, then log in again")
    return Result(OK, "capture card", str(device))


def check_hdmi_signal(config: Config) -> Result:
    """Grab one frame, through the running server when there is one (the card
    accepts a single reader), otherwise directly."""
    try:
        if cli.port_open(config.host, config.port):
            frame = _frame_from_preview(config)
        else:
            frame = _frame_from_device(config.video_device)
    except Exception as exc:  # noqa: BLE001 - reported to the user as is
        return Result(WARN, "hdmi signal", f"no frame: {exc}", "is the console on and docked? is another program using the card?")
    if frame is None:
        return Result(WARN, "hdmi signal", "no frame", "is the console on and docked?")
    height, width = frame.shape[:2]
    if float(frame.std()) < 2.0:
        return Result(WARN, "hdmi signal", f"{width}x{height} but uniform image (no signal or console asleep)", "wake the console up")
    return Result(OK, "hdmi signal", f"{width}x{height}")


def _frame_from_preview(config: Config):
    import cv2
    import numpy as np

    with urllib.request.urlopen(f"http://{config.host}:{config.port}/preview", timeout=10) as response:
        buffer = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            buffer += response.read1(65536)
            start = buffer.find(b"\xff\xd8")
            end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if end >= 0:
                return cv2.imdecode(np.frombuffer(buffer[start:end + 2], np.uint8), cv2.IMREAD_COLOR)
    return None


def _frame_from_device(device: str):
    from .capture import open_v4l2

    source = open_v4l2(device)
    try:
        for _ in range(30):
            ok, frame = source.read()
            if ok and frame is not None:
                return frame
        return None
    finally:
        source.release()


def check_link_keys() -> Result:
    path = default_store_path()
    try:
        keys = json.loads(path.read_text())
    except FileNotFoundError:
        return Result(INFO, "pairing", "no stored key: the first switch_connect will pair (Controllers > Change Grip/Order)")
    except (OSError, ValueError) as exc:
        return Result(FAIL, "pairing", f"{path} unreadable: {exc}")
    if path.stat().st_mode & 0o077:
        return Result(WARN, "pairing", f"{path} is readable by others", f"chmod 600 {path}")
    consoles = ", ".join(sorted(keys)) or "none"
    return Result(OK, "pairing", f"key stored for {consoles}: direct reconnection")


def check_claude(config: Config) -> Result:
    claude = shutil.which("claude")
    if claude is None:
        return Result(INFO, "claude code", "not found on PATH; register the server in your MCP client by hand", config.url)
    try:
        result = subprocess.run([claude, "mcp", "get", "switch"], capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return Result(INFO, "claude code", "claude mcp get timed out")
    if result.returncode != 0:
        return Result(WARN, "claude code", "no MCP server named 'switch'",
                      f"claude mcp add --scope user --transport http switch {config.url}")
    if config.url not in result.stdout:
        return Result(WARN, "claude code", f"'switch' does not point to {config.url}",
                      f"claude mcp remove --scope user switch && claude mcp add --scope user --transport http switch {config.url}")
    return Result(OK, "claude code", f"'switch' -> {config.url}")


def collect() -> list[Result]:
    config_result, config = check_config()
    results = [config_result, check_adapter(), check_bluetoothd(), check_nuxbt(config), *check_service()]
    if config is not None:
        checks: list[Callable[[Config], Result]] = [check_listening, check_capture_device]
        results += [check(config) for check in checks]
        if results[-1].level == OK:
            results.append(check_hdmi_signal(config))
        results.append(check_link_keys())
        results.append(check_claude(config))
    return results


def run() -> int:
    results = collect()
    for result in results:
        print(f"{_MARKS[result.level]} {result.name}: {result.message}")
        if result.fix and result.level != OK:
            print(f"    → {result.fix}")
    failed = [r for r in results if r.level == FAIL]
    print()
    print(f"{len(failed)} problem(s) to fix." if failed else "Everything needed is in place.")
    return 1 if failed else 0
