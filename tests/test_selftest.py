"""The hardware acceptance test, run against a simulated console."""
import time

import anyio
import numpy as np
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from switch_mcp import selftest
from switch_mcp.capture import Capture
from switch_mcp.config import Config
from switch_mcp.controller import Pad
from switch_mcp.controller.fake import FakeBackend
from switch_mcp.server import build_server


class Screen:
    """Button test screen of the Switch 2: every press appends an icon, which stays
    after the release; the row shows the last ten presses."""

    def __init__(self, backend):
        self.backend = backend

    def read(self):
        time.sleep(0.005)
        presses, down = [], frozenset()
        for _, state in list(self.backend.history):
            presses += sorted(state.buttons - down)
            down = state.buttons
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for slot, button in enumerate(presses[-10:]):
            shade = 60 + 15 * sorted(selftest.DEFAULT_BUTTONS).index(button)
            frame[300:380, 100 * slot:100 * slot + 80] = shade
        return True, frame

    def release(self):
        pass


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(selftest, "_INPUT_DELAY_S", 0.01)
    monkeypatch.setattr(selftest, "_POLL_S", 0.01)
    monkeypatch.setattr(selftest, "_RECONNECT_PAUSE_S", 0.01)


def run(backend, **kwargs):
    pad = Pad(backend)
    capture = Capture("fake", opener=lambda device: Screen(backend))
    server = build_server(Config(backend="fake"), pad, backend, capture)
    lines = []

    async def main():
        async with create_connected_server_and_client_session(server) as session:
            options = {"presses": 4, "connect_timeout_s": 1, "reconnect_timeout_s": 1, **kwargs}
            return await selftest.run_selftest(session, out=lines.append, **options)

    report = anyio.run(main)
    pad.close()
    return report, lines


def test_passes_on_a_working_console():
    prompts = []
    report, lines = run(FakeBackend(), confirm=prompts.append)
    assert report.passed, lines
    assert [name for name, _, _ in report.checks] == [
        "Connexion", "Appuis vérifiés par capture", "Déconnexion", "Reconnexion directe",
    ]
    assert "4/4" in lines[1]
    assert prompts and "Tester les boutons" in prompts[0]


class DeafConsole(FakeBackend):
    """Connected, but the console never sees B."""

    def apply(self, state):
        super().apply(type(state)(buttons=state.buttons - {"B"}, left=state.left, right=state.right))


def test_reports_presses_the_console_missed():
    report, lines = run(DeafConsole())
    assert not report.passed
    assert "3/4" in lines[1] and "B n°2 pas vu" in lines[1]


class StuckConsole(FakeBackend):
    """The release of A never reaches the console."""

    def apply(self, state):
        stuck = any("A" in s.buttons for _, s in self.history)
        super().apply(type(state)(buttons=state.buttons | ({"A"} if stuck else set()), left=state.left, right=state.right))


def test_reports_a_button_that_stays_down():
    report, lines = run(StuckConsole(), buttons=("A",))
    assert not report.passed
    assert "1/4" in lines[1] and "A n°2 pas vu" in lines[1]


def test_more_presses_than_the_row_holds():
    report, lines = run(FakeBackend(), presses=25)
    assert report.passed, lines


class ForgetfulConsole(FakeBackend):
    """Pairs once, then forgets the controller, as the Switch 2 sometimes does."""

    connections = 0

    def connect(self):
        self.connections += 1
        self.state = "connected" if self.connections == 1 else "connecting"


def test_reports_a_console_that_forgot_the_controller():
    report, lines = run(ForgetfulConsole())
    assert not report.passed
    assert report.checks[-1][0] == "Reconnexion directe" and not report.checks[-1][1]
    assert "oublié la manette" in lines[-1]


def test_stops_when_the_pc_is_not_ready():
    backend = FakeBackend()
    backend.status = lambda: {"state": "idle", "backend": "nuxbt", "setup_error": "missing CAP_NET_RAW"}
    report, lines = run(backend)
    assert not report.passed and "CAP_NET_RAW" in lines[0]


def test_changed_share_ignores_identical_frames():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert selftest.changed_share(frame, frame.copy()) == 0
    lit = frame.copy()
    lit[100:200, 0:80] = 255
    assert selftest.changed_share(frame, lit) > selftest.CHANGED_SHARE


def test_cli_reports_an_unreachable_server(capsys):
    assert selftest.main(["--url", "http://127.0.0.1:9/mcp", "--yes"]) == 2
    assert "injoignable" in capsys.readouterr().err
