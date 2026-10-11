"""What an agent sees: tool schemas, actionable errors, safety on a vanished client."""
import json
import time

import anyio
import cv2
import numpy as np
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from switch_mcp.capture import Capture, CaptureError
from switch_mcp.config import Config
from switch_mcp.controller import BUTTONS, Pad
from switch_mcp.controller.fake import FakeBackend
from switch_mcp.server import Button, build_server


class Source:
    def read(self):
        time.sleep(0.002)
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        frame[:540, 960:] = 255  # top-right quarter is white
        return True, frame

    def release(self):
        pass


def make(opener=lambda device: Source()):
    backend = FakeBackend()
    pad = Pad(backend)
    capture = Capture("fake", opener=opener)
    return build_server(Config(backend="fake"), pad, backend, capture), backend


def run_session(server, body):
    async def main():
        async with create_connected_server_and_client_session(server) as session:
            return await body(session)

    return anyio.run(main)


def call(server, name, arguments=None):
    return run_session(server, lambda session: session.call_tool(name, arguments or {}))


def text(result):
    return " ".join(block.text for block in result.content if block.type == "text")


def schemas(server):
    return {tool.name: tool for tool in anyio.run(server.list_tools)}


# -- schemas -----------------------------------------------------------------


def test_button_enum_matches_controller():
    assert set(Button.__args__) == BUTTONS


def test_every_tool_is_documented_and_annotated():
    server, _ = make()
    for tool in schemas(server).values():
        assert len(tool.description) > 40, tool.name
        assert tool.annotations is not None and tool.annotations.readOnlyHint is not None, tool.name
    tools = schemas(server)
    assert tools["switch_status"].annotations.readOnlyHint
    assert tools["switch_screenshot"].annotations.readOnlyHint
    assert not tools["switch_press"].annotations.readOnlyHint


def test_button_names_and_bounds_are_in_the_schema():
    tools = schemas(make()[0])
    press = tools["switch_press"].inputSchema["properties"]
    assert set(press["buttons"]["items"]["enum"]) == BUTTONS
    assert (press["duration_ms"]["minimum"], press["duration_ms"]["maximum"]) == (20, 5000)
    stick = tools["switch_stick"].inputSchema["properties"]
    assert (stick["x"]["minimum"], stick["x"]["maximum"]) == (-1, 1)
    assert "up" in stick["y"]["description"]
    hold = tools["switch_hold"].inputSchema["properties"]["seconds"]
    assert hold["anyOf"][0]["maximum"] == 30


def test_sequence_step_schema_is_strict():
    schema = schemas(make()[0])["switch_sequence"].inputSchema
    step = schema["$defs"]["Step"]
    assert step["required"] == ["ms"] and step["additionalProperties"] is False
    assert schema["properties"]["steps"]["maxItems"] == 64


@pytest.mark.parametrize("name, arguments", [
    ("switch_press", {"buttons": ["NOPE"]}),
    ("switch_press", {"buttons": []}),
    ("switch_press", {"buttons": ["A"], "duration_ms": 10}),
    ("switch_stick", {"side": "left", "x": 2, "y": 0}),
    ("switch_hold", {"buttons": ["A"], "seconds": 99}),
    ("switch_sequence", {"steps": [{"ms": 100, "oops": 1}]}),
    ("switch_screenshot", {"crop": [0.5, 0.5, 0.2]}),
])
def test_invalid_arguments_are_rejected_before_the_console(name, arguments):
    server, backend = make()
    backend.connect()
    result = call(server, name, arguments)
    assert result.isError
    assert backend.history == []


def test_prompt_starts_a_session():
    server, _ = make()

    async def body(session):
        prompts = await session.list_prompts()
        assert [p.name for p in prompts.prompts] == ["start_session"]
        return await session.get_prompt("start_session", {"goal": "finish level 1"})

    message = run_session(server, body).messages[0].content.text
    assert "finish level 1" in message
    assert message.index("switch_status") < message.index("switch_connect") < message.index("switch_screenshot")


# -- actionable errors -------------------------------------------------------


def test_status_says_what_to_do():
    server, backend = make()
    status = json.loads(text(call(server, "switch_status")))
    assert status["state"] == "idle" and "switch_connect" in status["next_step"]
    assert status["hdmi_signal"] is None
    backend.connect()
    status = json.loads(text(call(server, "switch_status")))
    assert status["next_step"].startswith("Ready")


def test_input_while_disconnected_tells_to_connect():
    server, backend = make()
    result = call(server, "switch_press", {"buttons": ["A"]})
    assert result.isError
    assert "idle" in text(result) and "switch_connect" in text(result)
    assert "Traceback" not in text(result)


def test_input_while_connecting_tells_to_wait():
    server, backend = make()
    backend.state = "connecting"
    result = call(server, "switch_hold", {"buttons": ["A"]})
    assert result.isError and "Change Grip/Order" in text(result)


def test_setup_error_points_to_doctor():
    server, backend = make()
    backend.status = lambda: {"state": "idle", "backend": "nuxbt", "setup_error": "bluetoothd has its plugins"}
    status = json.loads(text(call(server, "switch_status")))
    assert "switch-mcp doctor" in status["next_step"]


def test_connect_when_already_connected_is_not_an_error():
    server, backend = make()
    backend.connect()

    def refuse():
        raise RuntimeError("Controller already connected")

    backend.connect = refuse
    result = call(server, "switch_connect")
    assert not result.isError and "Already connected" in text(result)


def test_capture_error_tells_the_user_what_to_check(monkeypatch):
    monkeypatch.setattr("switch_mcp.capture.FIRST_FRAME_TIMEOUT_S", 0.1)
    monkeypatch.setattr("switch_mcp.capture._REOPEN_DELAY_S", 0.01)

    def busy(device):
        raise CaptureError("Cannot open fake; is another program using the capture card?")

    server, _ = make(opener=busy)
    result = call(server, "switch_screenshot")
    assert result.isError
    assert "docked" in text(result) and "/preview" in text(result)


# -- screenshots -------------------------------------------------------------


def decode(result):
    import base64

    image = next(block for block in result.content if block.type == "image")
    return cv2.imdecode(np.frombuffer(base64.b64decode(image.data), np.uint8), cv2.IMREAD_COLOR)


def test_crop_zooms_on_a_region():
    server, _ = make()
    frame = decode(call(server, "switch_screenshot", {"crop": [0.5, 0, 0.5, 0.5]}))
    assert frame.shape == (720, 1280, 3)
    assert frame.mean() > 250  # only the white quarter


def test_status_reports_hdmi_signal_after_a_screenshot():
    server, _ = make()

    async def body(session):
        await session.call_tool("switch_screenshot", {})
        return await session.call_tool("switch_status", {})

    status = json.loads(text(run_session(server, body)))
    assert status["capture_open"] and status["hdmi_signal"] is True and status["capture_error"] is None


def test_sequence_tool_plays_typed_steps():
    server, backend = make()
    backend.connect()
    result = call(server, "switch_sequence", {"steps": [{"ms": 30, "buttons": ["A"], "left": [1, 0]}, {"ms": 20}]})
    assert not result.isError
    assert backend.history[0][1].left == (1.0, 0.0) and backend.current.idle


# -- client disappearing -----------------------------------------------------


def test_press_is_released_when_the_client_goes_away():
    server, backend = make()
    backend.connect()

    async def main():
        async with create_connected_server_and_client_session(server) as session:
            with anyio.move_on_after(0.1):
                await session.call_tool("switch_press", {"buttons": ["A"], "duration_ms": 400})
        # The session is closed mid-press, as when an agent is stopped.

    anyio.run(main)
    deadline = time.monotonic() + 2
    while not backend.current.idle and time.monotonic() < deadline:
        time.sleep(0.02)
    assert backend.current.idle
    assert [sorted(state.buttons) for _, state in backend.history] == [["A"], []]


def test_held_inputs_release_when_nobody_renews_them():
    backend = FakeBackend()
    pad = Pad(backend, default_hold_s=0.5)
    server = build_server(Config(backend="fake"), pad, backend, Capture("fake", opener=lambda d: Source()))
    backend.connect()

    async def body(session):
        await session.call_tool("switch_hold", {"buttons": ["ZR"]})
        await session.call_tool("switch_stick", {"side": "left", "x": 0, "y": 1})
        return backend.current

    held = run_session(server, body)
    assert held.buttons == {"ZR"} and held.left == (0.0, 1.0)
    # The agent stops calling: the watchdog lets go on its own.
    deadline = time.monotonic() + 3
    while not backend.current.idle and time.monotonic() < deadline:
        time.sleep(0.02)
    assert backend.current.idle
    pad.close()
