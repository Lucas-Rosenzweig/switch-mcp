"""MCP server: observe the Switch through HDMI capture, act through an emulated controller."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Literal

from anyio import sleep, to_thread
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from .capture import Capture, CaptureError
from .config import Config
from .controller import ControllerError, Pad, make_backend
from .controller.linkkeys import default_store_path

log = logging.getLogger("switch_mcp")

INSTRUCTIONS = (
    "You control a physical Nintendo Switch: an emulated Pro Controller sends inputs over "
    "Bluetooth and an HDMI capture card shows you the screen. "
    "Start with switch_status and follow its `next_step`. If the controller is not connected, "
    "call switch_connect and poll switch_status every second or two until state is 'connected' "
    "(it goes through connecting/reconnecting, then settling for about 3 s). "
    "Then work in a loop: look (switch_screenshot), act briefly, look again. "
    "An action is never assumed to have worked: check the next image. "
    "Pass screenshot=true on switch_press, switch_stick or switch_sequence to get the resulting "
    "image in the same call. Prefer short presses (about 100 ms) and switch_sequence for anything "
    "timing-sensitive. Held buttons and tilted sticks release themselves after a few seconds "
    "unless renewed, so a stalled agent never leaves the console with a stuck input. "
    "If a tool says something only the human can fix (console asleep, capture card busy, "
    "pairing menu needed), stop and tell the user exactly that."
)

Quality = Literal["fast", "normal", "detail"]
Button = Literal[
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "HOME", "CAPTURE",
    "UP", "DOWN", "LEFT", "RIGHT", "L_STICK", "R_STICK",
]

Buttons = Annotated[
    list[Button],
    Field(min_length=1, max_length=18, description=(
        "Buttons pressed together. UP/DOWN/LEFT/RIGHT are the d-pad, L_STICK/R_STICK click the "
        "sticks, PLUS/MINUS are +/-. Nintendo layout: A is the right face button, B the bottom one."
    )),
]
Axis = Annotated[float, Field(ge=-1, le=1, description="-1 to 1; +x is right, +y is up, 0 is centre")]
HoldSeconds = Annotated[
    float | None,
    Field(gt=0, le=30, description="Automatic release after this many seconds (default 5, max 30)"),
]
Screenshot = Annotated[bool, Field(description="Also return the image taken settle_ms after the action")]
SettleMs = Annotated[int, Field(ge=0, le=5000, description="Wait before that image, in ms (0-5000)")]
Crop = Annotated[
    list[Annotated[float, Field(ge=0, le=1)]] | None,
    Field(min_length=4, max_length=4, description=(
        "Zoom on a region: [x, y, width, height] as fractions of the picture, e.g. "
        "[0.75, 0, 0.25, 0.2] for the top-right corner. The region is enlarged to the quality's width."
    )),
]


class Step(BaseModel):
    """One complete controller state, held for `ms`."""

    model_config = ConfigDict(extra="forbid")
    ms: int = Field(ge=20, le=60000, description="How long this state lasts, in ms")
    buttons: list[Button] = Field(default_factory=list, description="Buttons down during this step")
    left: tuple[Axis, Axis] | None = Field(None, description="Left stick [x, y]; centred if omitted")
    right: tuple[Axis, Axis] | None = Field(None, description="Right stick [x, y]; centred if omitted")


# What to tell the agent for each connection state.
NEXT_STEPS = {
    "idle": "Call switch_connect.",
    "initializing": "Connection is starting. Poll switch_status in a second or two.",
    "connecting": (
        "Waiting for the console. If it has never been paired, the console must be on "
        "Controllers > Change Grip/Order (ask the user to open it). Poll switch_status."
    ),
    "reconnecting": "Reconnecting to the known console. Poll switch_status in a second or two.",
    "settling": "Connected; inputs are accepted in about 3 s. Poll switch_status.",
    "connected": "Ready. Take a screenshot, then act.",
    "crashed": "The controller link failed. Call switch_connect to start again.",
}


def next_step(status: dict) -> str:
    if status.get("setup_error"):
        return (
            "The PC is not ready for Bluetooth emulation: tell the user to run "
            f"`switch-mcp doctor`. Details: {status['setup_error']}"
        )
    return NEXT_STEPS.get(status.get("state"), "Poll switch_status.")


def controller_error(backend, exc: Exception) -> ToolError:
    """Turn a refused input into a sentence that says what to do next."""
    status = backend.status()
    if status.get("state") == "connected":
        # Refused for another reason, such as a sequence already playing.
        return ToolError(str(exc))
    return ToolError(f"The controller is {status.get('state')}, so inputs are refused. {next_step(status)}")


def build_server(config: Config, pad: Pad, backend, capture: Capture) -> FastMCP:
    mcp = FastMCP("Nintendo Switch", instructions=INSTRUCTIONS, host=config.host, port=config.port)

    # Tools that wait (a press, a sequence, a frame) run in a worker thread so
    # other calls, such as steering a stick meanwhile, are still served.

    def screenshot_now(quality: Quality, crop=None) -> bytes:
        try:
            return capture.screenshot(quality, tuple(crop) if crop else None)
        except CaptureError as exc:
            raise ToolError(
                f"No picture: {exc}. Ask the user to check that the Switch is docked and awake, and "
                "that no other program (OBS, ffplay) has the capture card open; the live view is at "
                f"http://{config.host}:{config.port}/preview instead."
            ) from exc

    def inputs(action):
        """Run an input action, translating refusals into actionable errors."""
        try:
            return action()
        except ControllerError as exc:
            raise controller_error(backend, exc) from exc
        except RuntimeError as exc:
            # The backend lost the link between the state check and the send.
            raise controller_error(backend, exc) from exc

    def result(payload: dict, screenshot: bool, settle_ms: int, quality: Quality):
        """Return the payload, followed by a screenshot taken after `settle_ms`."""
        if not screenshot:
            return payload
        time.sleep(max(0, min(settle_ms, 5000)) / 1000.0)
        return [payload, Image(data=screenshot_now(quality), format="jpeg")]

    def describe(state) -> dict:
        return {"held_buttons": sorted(state.buttons), "left_stick": state.left, "right_stick": state.right}

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    acts = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=read_only)
    def switch_status() -> dict:
        """Where things stand and what to do next. Call it first, and poll it after switch_connect.

        `state` is idle, connecting, reconnecting, settling, connected or crashed; inputs are
        accepted only when it is 'connected'. `next_step` says what to do. Also reports the
        inputs currently held, whether the HDMI picture is coming through (`hdmi_signal`,
        null until a screenshot has been taken) and the last capture or controller error.
        """
        status = backend.status()
        return {**status, "next_step": next_step(status), **describe(pad.snapshot()), **capture.status()}

    @mcp.tool(annotations=acts)
    def switch_connect() -> dict:
        """Connect the emulated controller to the console. Returns at once; poll switch_status.

        A console paired before is reconnected directly in a few seconds, as long as it is
        on and not showing the pairing menu. The very first time, the user must open
        Controllers > Change Grip/Order on the console; it then has three minutes to find
        the controller. Calling this while connecting restarts the attempt.
        """
        pad.release_all()
        try:
            backend.connect()
        except RuntimeError as exc:
            status = backend.status()
            if status.get("state") in ("connected", "settling"):
                return {**status, "next_step": "Already connected. " + next_step(status)}
            raise ToolError(
                f"Cannot start the connection: {exc}. If it is about Bluetooth or permissions, tell the "
                "user to run `switch-mcp doctor`."
            ) from exc
        status = backend.status()
        return {**status, "next_step": next_step(status)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    def switch_disconnect() -> dict:
        """Release every input and disconnect the controller. switch_connect reconnects it."""
        pad.release_all()
        backend.disconnect()
        status = backend.status()
        return {**status, "next_step": next_step(status)}

    @mcp.tool(annotations=read_only)
    async def switch_screenshot(quality: Quality = "normal", crop: Crop = None) -> Image:
        """A fresh image of the console screen, taken after this call (never a cached one).

        quality: fast = 960 px wide, normal = 1280 px (default), detail = native resolution.
        Use crop to read small text or icons instead of a full detail image.
        """
        return Image(data=await to_thread.run_sync(screenshot_now, quality, crop), format="jpeg")

    @mcp.tool(annotations=acts)
    async def switch_press(
        buttons: Buttons,
        duration_ms: Annotated[int, Field(ge=20, le=5000, description="How long to hold, in ms")] = 100,
        screenshot: Screenshot = False,
        settle_ms: SettleMs = 300,
        quality: Quality = "normal",
    ):
        """Press one or more buttons together, wait duration_ms, then release them.

        Use about 100 ms for a menu step; one press moves a menu cursor by one item. Inputs
        already held (a tilted stick, a held button) stay as they are, so this combines with
        switch_hold and switch_stick. Example: buttons=["A"] confirms, ["B"] goes back.
        """
        def run():
            state = inputs(lambda: pad.press(buttons, duration_ms / 1000.0))
            return result(describe(state), screenshot, settle_ms, quality)

        return await to_thread.run_sync(run)

    @mcp.tool(annotations=acts)
    def switch_hold(buttons: Buttons, seconds: HoldSeconds = None) -> dict:
        """Hold buttons down without blocking, e.g. to run or charge an attack while you do
        other things. They stay down until switch_release or until `seconds` pass. Call again
        before then to renew."""
        return describe(inputs(lambda: pad.hold(buttons, seconds)))

    @mcp.tool(annotations=acts)
    def switch_release(buttons: Buttons) -> dict:
        """Release buttons held with switch_hold. Always allowed, even when disconnected."""
        return describe(pad.release(buttons))

    @mcp.tool(annotations=acts)
    async def switch_stick(
        side: Literal["left", "right"],
        x: Axis,
        y: Axis,
        seconds: HoldSeconds = None,
        screenshot: Screenshot = False,
        settle_ms: SettleMs = 300,
        quality: Quality = "normal",
    ):
        """Tilt a stick without blocking: x=1 is right, y=1 is up (forward), (0, 0) centres it.

        It stays tilted until set back to (0, 0) or until `seconds` pass; call again to renew
        or steer. The left stick usually moves the character, the right one the camera.
        For a precise distance, prefer switch_sequence with a fixed duration.
        """
        state = inputs(lambda: pad.set_stick(side, x, y, seconds))
        return await to_thread.run_sync(result, describe(state), screenshot, settle_ms, quality)

    @mcp.tool(annotations=acts)
    async def switch_sequence(
        steps: Annotated[list[Step], Field(min_length=1, max_length=64)],
        screenshot: Screenshot = False,
        settle_ms: SettleMs = 300,
        quality: Quality = "normal",
    ):
        """Play precisely timed controller states in one call, then release everything.

        Each step is the complete state for its duration: anything not listed is released.
        A step with only "ms" is a pause. Up to 64 steps and 60 s in total.
        Example, run right then jump while running:
        [{"ms": 400, "left": [1, 0]}, {"ms": 150, "left": [1, 0], "buttons": ["B"]}]
        Example, move down two menu items:
        [{"ms": 100, "buttons": ["DOWN"]}, {"ms": 150}, {"ms": 100, "buttons": ["DOWN"]}]
        """
        plan = [step.model_dump(exclude_none=True) for step in steps]

        def run():
            total = inputs(lambda: pad.sequence(plan))
            return result({"played_ms": round(total * 1000), "steps": len(plan)}, screenshot, settle_ms, quality)

        return await to_thread.run_sync(run)

    @mcp.tool(annotations=acts)
    def switch_release_all() -> dict:
        """Release every button and centre both sticks. Always allowed; use it if an input seems stuck."""
        return describe(pad.release_all())

    @mcp.prompt(name="start_session", description="Check the setup, connect the controller and look at the screen.")
    def start_session(goal: str = "") -> str:
        task = f"Your goal: {goal}\n\n" if goal else ""
        return (
            f"{task}Start a session on the Nintendo Switch:\n"
            "1. Call switch_status and read `next_step`.\n"
            "2. If the controller is not connected, call switch_connect, then poll switch_status "
            "every second or two until state is 'connected'. If it stays 'connecting' for more than "
            "20 s, ask the user to open Controllers > Change Grip/Order on the console.\n"
            "3. Take a switch_screenshot and describe what is on screen before doing anything.\n"
            "Then work in short steps: act, look, and check the result before the next action."
        )

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
