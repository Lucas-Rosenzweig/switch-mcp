"""The switch-mcp command.

    switch-mcp serve          run the MCP server in the foreground
    switch-mcp start | stop   start or stop the installed service (no password)
    switch-mcp status         service, Bluetooth mode and MCP address
    switch-mcp doctor         check the whole setup and say what to fix
    switch-mcp bt on|off|status
                              switch bluetoothd in and out of emulation mode by hand
    switch-mcp config         show the effective configuration
    switch-mcp selftest       acceptance test on the real console, through the running server
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .config import Config, ConfigError, config_path

SERVICE = "switch-mcp.service"
# Installed by install.sh, root-owned: the service runs it as root, so it must
# not be a file the user can edit (polkit lets the user start the service
# without a password).
BT_HELPER = Path("/usr/local/libexec/switch-mcp/bluetooth-mode")
BT_OVERRIDE = Path("/run/systemd/system/bluetooth.service.d/switch-mcp.conf")
START_TIMEOUT_S = 30.0


def _repo_helper() -> Path:
    return Path(__file__).resolve().parents[2] / "deploy" / "bluetooth-mode"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=False)


def service_installed() -> bool:
    return _systemctl("cat", SERVICE).returncode == 0


def service_active() -> bool:
    return _systemctl("is-active", "--quiet", SERVICE).returncode == 0


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bt_mode_on() -> bool:
    return BT_OVERRIDE.exists()


# -- commands ---------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    config = Config.load()
    if not service_installed():
        print(f"{SERVICE} is not installed. Run ./install.sh from the project first.", file=sys.stderr)
        return 1
    result = _systemctl("start", SERVICE)
    if result.returncode != 0:
        print(result.stderr.strip() or f"systemctl start {SERVICE} failed", file=sys.stderr)
        print("See: journalctl -u switch-mcp -e   or   switch-mcp doctor", file=sys.stderr)
        return 1
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if port_open(config.host, config.port):
            print(f"Ready: {config.url}")
            return 0
        if not service_active():
            break
        time.sleep(0.5)
    print(f"The service did not start listening on {config.host}:{config.port}.", file=sys.stderr)
    print("See: journalctl -u switch-mcp -e   or   switch-mcp doctor", file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    result = _systemctl("stop", SERVICE)
    if result.returncode != 0:
        print(result.stderr.strip() or f"systemctl stop {SERVICE} failed", file=sys.stderr)
        return 1
    print("Stopped; Bluetooth is back to normal.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Config.load()
    if not service_installed():
        service = "not installed"
    else:
        service = _systemctl("is-active", SERVICE).stdout.strip() or "unknown"
    listening = port_open(config.host, config.port)
    print(f"service:   {service}")
    print(f"bluetooth: {'emulation mode' if bt_mode_on() else 'normal'}")
    print(f"mcp:       {config.url} ({'listening' if listening else 'not listening'})")
    return 0 if listening else 3


def cmd_bt(args: argparse.Namespace) -> int:
    if args.mode == "status":
        print("on" if bt_mode_on() else "off")
        return 0
    if args.mode == "off" and service_active():
        print("The service is running and needs emulation mode; use `switch-mcp stop` instead.", file=sys.stderr)
        return 1
    helper = BT_HELPER if BT_HELPER.exists() else _repo_helper()
    command = [str(helper), args.mode]
    if not _is_root():
        command = ["sudo", *command]
    return subprocess.run(command, check=False).returncode


def cmd_config(args: argparse.Namespace) -> int:
    config = Config.load()
    if args.url:
        print(config.url)
        return 0
    path = config_path()
    print(f"# {path}{'' if path.exists() else ' (absent, defaults in use)'}")
    for name, value in asdict(config).items():
        if value is None:
            print(f"# {name} = (unset)")
        else:
            print(f"{name} = {_toml(value)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run

    return run()


def _toml(value: object) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _is_root() -> bool:
    return os.geteuid() == 0


# -- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switch-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Handled before parsing, so its options reach the server's own parser.
    commands.add_parser("serve", help="run the MCP server in the foreground (serve --help for its options)")

    commands.add_parser("start", help="start the service and wait until it listens").set_defaults(func=cmd_start)
    commands.add_parser("stop", help="stop the service and restore normal Bluetooth").set_defaults(func=cmd_stop)
    commands.add_parser("status", help="service, Bluetooth mode and MCP address").set_defaults(func=cmd_status)
    commands.add_parser("doctor", help="check the setup and say what to fix").set_defaults(func=cmd_doctor)

    bt = commands.add_parser("bt", help="bluetoothd emulation mode by hand (asks for sudo)")
    bt.add_argument("mode", choices=["on", "off", "status"])
    bt.set_defaults(func=cmd_bt)

    from . import selftest

    check = commands.add_parser("selftest", help="acceptance test on the real console (server must be running)")
    selftest.add_arguments(check)
    check.set_defaults(func=selftest.run)

    config = commands.add_parser("config", help="show the effective configuration")
    config.add_argument("--url", action="store_true", help="print only the MCP address")
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # The former invocation `switch-mcp --transport ...` still means serve.
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["serve", *argv]
    try:
        if argv and argv[0] == "serve":
            from . import server

            server.main(argv[1:])
            return 0
        args = build_parser().parse_args(argv)
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
