"""MCP server: observe the Switch through HDMI capture, act through an emulated controller."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from anyio import sleep, to_thread
from mcp.server.fastmcp import FastMCP, Image
from starlette.responses import StreamingResponse

from .capture import Capture
from .config import Config
from .controller import Pad, make_backend
from .controller.linkkeys import default_store_path

log = logging.getLogger("switch_mcp")

INSTRUCTIONS = (
    "Control a physical Nintendo Switch with an emulated Pro Controller and watch its "
    "HDMI output. Start with switch_status; if the controller is not connected call "
    "switch_connect and poll switch_status until it says connected. Then loop: look (switch_screenshot), act, "
    "look again. Never assume an action worked without seeing the result; pass "
    "screenshot=true on an action to get the resulting image in the same call. "
    "Buttons: A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE UP DOWN LEFT RIGHT (d-pad) "
    "L_STICK R_STICK (stick clicks). Stick axes are in [-1, 1], +x right, +y up. "
    "Held inputs release themselves after a few seconds unless renewed."
)

Quality = Literal["fast", "normal", "detail"]


def build_server(config: Config, pad: Pad, backend, capture: Capture) -> FastMCP:
    mcp = FastMCP("Nintendo Switch", instructions=INSTRUCTIONS, host=config.host, port=config.port)

    # Tools that wait (a press, a sequence, a frame) run in a worker thread so
    # other calls, such as steering a stick meanwhile, are still served.

    def result(payload: dict, screenshot: bool, settle_ms: int, quality: Quality):
        """Return the payload, followed by a screenshot taken after `settle_ms`."""
        if not screenshot:
            return payload
        time.sleep(max(0, min(settle_ms, 5000)) / 1000.0)
        return [payload, Image(data=capture.screenshot(quality), format="jpeg")]

    def describe(state) -> dict:
        return {"held_buttons": sorted(state.buttons), "left_stick": state.left, "right_stick": state.right}

    @mcp.tool()
    def switch_status() -> dict:
        """Controller connection state (idle, connecting, reconnecting, settling, connected,
        crashed; inputs are accepted only once it is connected),
        the inputs currently held, and whether the capture device is open."""
        return {**backend.status(), **describe(pad.snapshot()), "capture_open": capture.running}

    @mcp.tool()
    def switch_connect() -> dict:
        """Connect the emulated controller. Returns at once; poll switch_status until 'connected'.

        A console paired before is reconnected directly. Otherwise the console must be
        showing Controllers > Change Grip/Order, and has three minutes to find the
        controller; call switch_connect again to restart a pairing that timed out.
        """
        pad.release_all()
        backend.connect()
        return backend.status()

    @mcp.tool()
    def switch_disconnect() -> dict:
        """Release everything and disconnect the emulated controller from the console."""
        pad.release_all()
        backend.disconnect()
        return backend.status()

    @mcp.tool()
    async def switch_screenshot(quality: Quality = "normal") -> Image:
        """Current image of the console. fast = 960px, normal = 1280px, detail = native
        resolution for small text."""
        return Image(data=await to_thread.run_sync(capture.screenshot, quality), format="jpeg")

    @mcp.tool()
    async def switch_press(
        buttons: list[str],
        duration_ms: int = 100,
        screenshot: bool = False,
        settle_ms: int = 300,
        quality: Quality = "normal",
    ):
        """Press one or more buttons together for 20-5000 ms, then release them.

        Inputs already held (a tilted stick, a held button) stay as they are, so this
        combines with switch_hold and switch_stick. With screenshot=true the image taken
        settle_ms after the release is returned too.
        """
        def run():
            state = pad.press(buttons, duration_ms / 1000.0)
            return result(describe(state), screenshot, settle_ms, quality)

        return await to_thread.run_sync(run)

    @mcp.tool()
    def switch_hold(buttons: list[str], seconds: float | None = None) -> dict:
        """Hold buttons down without blocking. They stay pressed until switch_release,
        or until `seconds` pass (default 5, maximum 30). Call again to renew."""
        return describe(pad.hold(buttons, seconds))

    @mcp.tool()
    def switch_release(buttons: list[str]) -> dict:
        """Release buttons held with switch_hold."""
        return describe(pad.release(buttons))

    @mcp.tool()
    async def switch_stick(
        side: Literal["left", "right"],
        x: float,
        y: float,
        seconds: float | None = None,
        screenshot: bool = False,
        settle_ms: int = 300,
        quality: Quality = "normal",
    ):
        """Tilt a stick without blocking. It stays tilted until set back to x=0, y=0,
        or until `seconds` pass (default 5, maximum 30). Call again to renew or steer."""
        state = pad.set_stick(side, x, y, seconds)
        return await to_thread.run_sync(result, describe(state), screenshot, settle_ms, quality)

    @mcp.tool()
    async def switch_sequence(
        steps: list[dict],
        screenshot: bool = False,
        settle_ms: int = 300,
        quality: Quality = "normal",
    ):
        """Play precisely timed steps in one call, then release everything.

        Each step is the complete controller state for its duration:
        {"ms": 200, "buttons": ["A"], "left": [0, 1], "right": [0, 0]}. Only "ms" is
        required; a step with nothing else is a pause. Up to 64 steps and 60 s in total.
        Example, run right then jump while running:
        [{"ms": 400, "left": [1, 0]}, {"ms": 150, "left": [1, 0], "buttons": ["B"]}]
        """
        def run():
            total = pad.sequence(steps)
            return result({"played_ms": round(total * 1000), "steps": len(steps)}, screenshot, settle_ms, quality)

        return await to_thread.run_sync(run)

    @mcp.tool()
    def switch_release_all() -> dict:
        """Release every button and centre both sticks."""
        return describe(pad.release_all())

    @mcp.custom_route("/preview", methods=["GET"])
    async def preview(request):
        """Live view for a person: the capture card accepts a single reader, so
        watching goes through the server instead of opening the device."""
        return StreamingResponse(
            preview_frames(capture), media_type=f"multipart/x-mixed-replace; boundary={PREVIEW_BOUNDARY}"
        )

    return mcp


PREVIEW_BOUNDARY = "frame"
PREVIEW_FPS = 20


async def preview_frames(capture: Capture, fps: float = PREVIEW_FPS):
    """Yield the capture as an endless MJPEG stream."""
    while True:
        started = time.monotonic()
        jpeg = await to_thread.run_sync(capture.screenshot, "normal")
        yield (
            f"--{PREVIEW_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n".encode()
            + jpeg + b"\r\n"
        )
        await sleep(max(0.0, 1.0 / fps - (time.monotonic() - started)))


def _log_to_file() -> None:
    """Keep a copy of the log next to the stored link keys, for diagnosis after the fact."""
    try:
        path = default_store_path().with_name("server.log")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2)
    except OSError:
        log.warning("Cannot write the log file", exc_info=True)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)


RELOAD_EXIT_STATUS = 75


def _exit_when_touched(path: Path, pad: Pad, backend) -> None:
    started = time.time()
    while True:
        time.sleep(1)
        try:
            touched = path.stat().st_mtime > started
        except OSError:
            touched = False
        if touched:
            log.info("%s was touched; exiting so the wrapper restarts the server", path)
            try:
                pad.close()
                backend.close()
            finally:
                os._exit(RELOAD_EXIT_STATUS)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="switch-mcp serve", description=__doc__)
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="streamable-http",
        help="stdio for a client that launches the server, streamable-http for a long-running service",
    )
    parser.add_argument(
        "--reload-file", metavar="PATH",
        help="exit with status 75 when PATH is touched, so a wrapper can restart the server (development)",
    )
    args = parser.parse_args(argv)
    # stdout carries the MCP protocol in stdio mode; logging goes to stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log_to_file()

    config = Config.load()
    backend = make_backend(config.backend)
    if hasattr(backend, "start"):
        try:
            backend.start()
        except RuntimeError as exc:
            # Keep serving: screenshots still work and switch_status explains what is missing.
            log.warning("Controller backend not ready: %s", exc)
    pad = Pad(backend, config.default_hold_s, config.max_hold_s)
    capture = Capture(config.video_device, config.capture_idle_s)
    if args.reload_file:
        threading.Thread(
            target=_exit_when_touched, args=(Path(args.reload_file), pad, backend), daemon=True, name="reload",
        ).start()
    try:
        build_server(config, pad, backend, capture).run(transport=args.transport)
    finally:
        pad.close()
        backend.close()


if __name__ == "__main__":
    main()
