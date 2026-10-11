import threading
import time

import pytest

from switch_mcp.controller import ControllerError, Pad, PadState
from switch_mcp.controller.fake import FakeBackend


@pytest.fixture
def backend():
    backend = FakeBackend()
    backend.connect()
    return backend


@pytest.fixture
def pad(backend):
    pad = Pad(backend, default_hold_s=5.0, max_hold_s=30.0)
    yield pad
    pad.close()


def states(backend):
    return [state for _, state in backend.history]


def test_press_releases_after_duration(pad, backend):
    start = time.monotonic()
    pad.press(["a"], 0.05)
    assert time.monotonic() - start >= 0.05
    assert states(backend) == [PadState(buttons=frozenset({"A"})), PadState()]


def test_press_keeps_other_inputs(pad, backend):
    pad.set_stick("left", 1.0, 0.0)
    pad.hold(["ZL"])
    pad.press(["B"], 0.03)
    assert backend.current == PadState(buttons=frozenset({"ZL"}), left=(1.0, 0.0))
    assert PadState(buttons=frozenset({"ZL", "B"}), left=(1.0, 0.0)) in states(backend)


def test_inputs_overlap_across_threads(pad, backend):
    thread = threading.Thread(target=pad.press, args=(["A"], 0.15))
    thread.start()
    time.sleep(0.05)
    pad.set_stick("left", 0.0, 1.0)
    thread.join()
    assert PadState(buttons=frozenset({"A"}), left=(0.0, 1.0)) in states(backend)


def test_watchdog_releases_expired_hold(pad, backend):
    pad.hold(["A"], 0.05)
    pad.set_stick("right", -1.0, 0.5, 0.05)
    time.sleep(0.2)
    assert backend.current.idle


def test_renewing_a_hold_extends_it(pad, backend):
    pad.hold(["A"], 0.1)
    time.sleep(0.06)
    pad.hold(["A"], 0.1)
    time.sleep(0.06)
    assert backend.current.buttons == {"A"}


def test_hold_duration_is_capped(backend):
    pad = Pad(backend, default_hold_s=5.0, max_hold_s=0.05)
    pad.hold(["A"], 999)
    time.sleep(0.2)
    assert backend.current.idle
    pad.close()


def test_centering_stick_releases_it(pad, backend):
    pad.set_stick("left", 0.5, 0.5)
    pad.set_stick("left", 0, 0)
    assert backend.current.idle


def test_sequence_plays_steps_then_releases(pad, backend):
    total = pad.sequence([
        {"ms": 40, "left": [1, 0]},
        {"ms": 40, "left": [1, 0], "buttons": ["B"]},
        {"ms": 20},
    ])
    assert total == pytest.approx(0.1)
    assert states(backend) == [
        PadState(left=(1.0, 0.0)),
        PadState(buttons=frozenset({"B"}), left=(1.0, 0.0)),
        PadState(),
    ]
    times = [t for t, _ in backend.history]
    assert times[1] - times[0] == pytest.approx(0.04, abs=0.02)


def test_sequence_releases_on_failure(pad, backend):
    def drop_link():
        time.sleep(0.03)
        backend.state = "crashed"

    threading.Thread(target=drop_link).start()
    with pytest.raises(ControllerError):
        pad.sequence([{"ms": 60, "buttons": ["A"]}, {"ms": 60, "buttons": ["B"]}])
    assert backend.current.idle


@pytest.mark.parametrize("call", [
    lambda pad: pad.press(["NOPE"]),
    lambda pad: pad.press([]),
    lambda pad: pad.press(["A"], 0.001),
    lambda pad: pad.press(["A"], 60),
    lambda pad: pad.set_stick("middle", 0, 0),
    lambda pad: pad.set_stick("left", 1.5, 0),
    lambda pad: pad.set_stick("left", float("nan"), 0),
    lambda pad: pad.hold(["A"], -1),
    lambda pad: pad.sequence([]),
    lambda pad: pad.sequence([{"buttons": ["A"]}]),
    lambda pad: pad.sequence([{"ms": 50, "oops": 1}]),
    lambda pad: pad.sequence([{"ms": 40000}, {"ms": 40000}]),
])
def test_invalid_input_is_rejected(pad, backend, call):
    with pytest.raises(ValueError):
        call(pad)
    assert backend.history == []


def test_inputs_refused_when_disconnected(pad, backend):
    backend.state = "connecting"
    with pytest.raises(ControllerError):
        pad.press(["A"])
    with pytest.raises(ControllerError):
        pad.set_stick("left", 1, 0)
    assert backend.history == []
    # Releasing must always be possible.
    pad.release_all()
