import anyio
import numpy as np

from switch_mcp.capture import Capture
from switch_mcp.config import Config
from switch_mcp.controller import Pad
from switch_mcp.controller.fake import FakeBackend
from switch_mcp.server import build_server


class Source:
    def read(self):
        return True, np.zeros((1080, 1920, 3), dtype=np.uint8)

    def release(self):
        pass


def make():
    backend = FakeBackend()
    pad = Pad(backend)
    capture = Capture("fake", opener=lambda device: Source())
    return build_server(Config(backend="fake"), pad, backend, capture), backend


def call(server, name, arguments=None):
    async def run():
        return await server.call_tool(name, arguments or {})

    return anyio.run(run)


def test_tools_are_exposed():
    server, _ = make()
    names = {tool.name for tool in anyio.run(server.list_tools)}
    assert names == {
        "switch_status", "switch_connect", "switch_disconnect", "switch_screenshot", "switch_press", "switch_hold",
        "switch_release", "switch_stick", "switch_sequence", "switch_release_all",
    }


def test_connect_then_press_with_screenshot():
    server, backend = make()
    call(server, "switch_connect")
    content = call(server, "switch_press", {"buttons": ["A"], "duration_ms": 30, "screenshot": True, "settle_ms": 0})
    blocks = content[0] if isinstance(content, tuple) else content
    assert {block.type for block in blocks} == {"text", "image"}
    assert [sorted(state.buttons) for _, state in backend.history] == [["A"], []]


def test_hold_and_status():
    server, backend = make()
    call(server, "switch_connect")
    call(server, "switch_stick", {"side": "left", "x": 1, "y": 0})
    call(server, "switch_hold", {"buttons": ["B"]})
    assert backend.current.left == (1.0, 0.0) and backend.current.buttons == {"B"}
    call(server, "switch_release_all")
    assert backend.current.idle


def test_preview_stream_yields_jpeg_parts():
    from switch_mcp.server import preview_frames

    capture = Capture("fake", opener=lambda device: Source())

    async def first_two():
        parts = []
        async for part in preview_frames(capture, fps=100):
            parts.append(part)
            if len(parts) == 2:
                return parts

    for part in anyio.run(first_two):
        header, _, body = part.partition(b"\r\n\r\n")
        assert header.startswith(b"--frame\r\nContent-Type: image/jpeg")
        assert body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9\r\n")
