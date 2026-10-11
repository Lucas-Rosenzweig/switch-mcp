"""Acceptance test on the real console, through the running server.

It talks to the server over MCP exactly as an agent does, so it checks the
whole chain: Bluetooth link, inputs reaching the console, HDMI capture and the
direct reconnection. Run it with the console on the "Test Input Devices >
Test Controller Buttons" screen (*Tester les périphériques d'entrée >
Tester les boutons*), where a held button lights up on screen.

    switch-mcp selftest            # the server must already be running
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import anyio
import cv2
import numpy as np

DEFAULT_URL = "http://127.0.0.1:8765/mcp"
# Buttons that do not leave the button test screen.
DEFAULT_BUTTONS = ("A", "B", "X", "Y", "UP", "DOWN", "LEFT", "RIGHT", "ZL", "ZR")
DEFAULT_PRESSES = 20
CONNECT_TIMEOUT_S = 180.0
RECONNECT_TIMEOUT_S = 30.0
# A held button must change at least this share of the (downscaled) picture,
# and the picture must come back once it is released.
CHANGED_PIXEL_DELTA = 30
CHANGED_SHARE = 0.0005
_INPUT_DELAY_S = 0.25
_POLL_S = 1.0
# Lets the console notice the disconnection before reconnecting.
_RECONNECT_PAUSE_S = 2.0


class SelftestFailure(RuntimeError):
    pass


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def add(self, name: str, ok: bool, detail: str, out: Callable[[str], None]) -> None:
        self.checks.append((name, ok, detail))
        out(f"[{'OK' if ok else 'ÉCHEC'}] {name} : {detail}")


def changed_share(before: np.ndarray, after: np.ndarray) -> float:
    """Share of pixels that differ visibly between two frames."""
    small = [cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (320, 180), interpolation=cv2.INTER_AREA)
             for f in (before, after)]
    return float(np.count_nonzero(cv2.absdiff(*small) > CHANGED_PIXEL_DELTA)) / small[0].size


class Client:
    """The few MCP calls the self-test needs, on top of a ClientSession."""

    def __init__(self, session) -> None:
        self._session = session

    async def call(self, name: str, arguments: dict | None = None):
        result = await self._session.call_tool(name, arguments or {})
        if result.isError:
            text = " ".join(getattr(block, "text", "") for block in result.content)
            raise SelftestFailure(f"{name} : {text}")
        return result.content

    async def json(self, name: str, arguments: dict | None = None) -> dict:
        content = await self.call(name, arguments)
        return json.loads(content[0].text)

    async def frame(self) -> np.ndarray:
        content = await self.call("switch_screenshot", {"quality": "fast"})
        data = base64.b64decode(content[0].data)
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise SelftestFailure("switch_screenshot : image illisible")
        return frame

    async def wait_connected(self, timeout_s: float) -> tuple[bool, float, str]:
        """Poll until connected; return (connected, seconds, last state)."""
        started = time.monotonic()
        state = "?"
        while time.monotonic() - started < timeout_s:
            state = (await self.json("switch_status")).get("state", "?")
            if state == "connected":
                return True, time.monotonic() - started, state
            await anyio.sleep(_POLL_S)
        return False, time.monotonic() - started, state


async def run_selftest(
    session,
    *,
    presses: int = DEFAULT_PRESSES,
    buttons: tuple[str, ...] = DEFAULT_BUTTONS,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    reconnect_timeout_s: float = RECONNECT_TIMEOUT_S,
    confirm: Callable[[str], None] = lambda message: None,
    out: Callable[[str], None] = print,
) -> Report:
    client = Client(session)
    report = Report()
    try:
        status = await client.json("switch_status")
        if status.get("setup_error"):
            report.add("Préparation du PC", False, status["setup_error"], out)
            return report

        # 1. Connection, direct or by pairing.
        if status.get("state") != "connected":
            await client.json("switch_connect")
        ok, seconds, state = await client.wait_connected(connect_timeout_s)
        report.add("Connexion", ok, f"{seconds:.1f} s" if ok else f"toujours « {state} » après {seconds:.0f} s", out)
        if not ok:
            return report

        # 2. Inputs, checked on screen.
        confirm("Ouvre sur la Switch : Paramètres > Manettes et accessoires > Tester les périphériques "
                "d'entrée > Tester les boutons, puis appuie sur Entrée.")
        failures = []
        for index in range(presses):
            button = buttons[index % len(buttons)]
            idle = await client.frame()
            await client.call("switch_hold", {"buttons": [button], "seconds": 3})
            await anyio.sleep(_INPUT_DELAY_S)
            held = await client.frame()
            await client.call("switch_release", {"buttons": [button]})
            await anyio.sleep(_INPUT_DELAY_S)
            released = await client.frame()
            seen, back = changed_share(idle, held), changed_share(idle, released)
            if seen < CHANGED_SHARE:
                failures.append(f"{button} n°{index + 1} pas vu à l'écran")
            elif back >= CHANGED_SHARE:
                failures.append(f"{button} n°{index + 1} pas relâché à l'écran")
        report.add(
            "Appuis vérifiés par capture", not failures,
            f"{presses - len(failures)}/{presses}" + (f" ({'; '.join(failures)})" if failures else ""), out,
        )

        # 3. Disconnect, then direct reconnection without the pairing menu.
        await client.call("switch_release_all")
        await client.json("switch_disconnect")
        report.add("Déconnexion", (await client.json("switch_status")).get("state") == "idle", "état idle", out)
        await anyio.sleep(_RECONNECT_PAUSE_S)
        await client.json("switch_connect")
        ok, seconds, state = await client.wait_connected(reconnect_timeout_s)
        detail = f"{seconds:.1f} s" if ok else (
            f"toujours « {state} » après {seconds:.0f} s : la Switch a sans doute oublié la manette "
            "(voir docs/depannage.md)"
        )
        report.add("Reconnexion directe", ok, detail, out)
    except SelftestFailure as exc:
        report.add("Appel MCP", False, str(exc), out)
    finally:
        try:
            await client.call("switch_release_all")
        except Exception:
            pass
    return report


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL, help=f"adresse MCP du serveur (défaut {DEFAULT_URL})")
    parser.add_argument("--presses", type=int, default=DEFAULT_PRESSES, help="nombre d'appuis vérifiés")
    parser.add_argument("--buttons", default=",".join(DEFAULT_BUTTONS), help="boutons à tester, séparés par des virgules")
    parser.add_argument("--yes", action="store_true", help="ne pas attendre Entrée avant les appuis")


def run(args: argparse.Namespace) -> int:
    """Entry point for `switch-mcp selftest`; returns the process exit status."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    buttons = tuple(b.strip().upper() for b in args.buttons.split(",") if b.strip())

    def confirm(message: str) -> None:
        if not args.yes:
            input(message + " ")

    async def main() -> Report:
        async with streamable_http_client(args.url) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            return await run_selftest(session, presses=args.presses, buttons=buttons, confirm=confirm)

    try:
        report = anyio.run(main)
    except Exception as exc:
        print(f"[ÉCHEC] Serveur injoignable à {args.url} : {exc}. Lance-le avec `switch-mcp start`.", file=sys.stderr)
        return 2
    print("Recette réussie : livrable." if report.passed else "Recette échouée.")
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="switch-mcp selftest", description=__doc__.splitlines()[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
