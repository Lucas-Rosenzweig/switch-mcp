import time

import cv2
import numpy as np
import pytest

from switch_mcp.capture import Capture, CaptureError


class Source:
    def __init__(self, frames_before_failure=None):
        self.remaining = frames_before_failure
        self.released = False
        self.count = 0

    def read(self):
        time.sleep(0.005)
        if self.remaining is not None:
            if self.remaining <= 0:
                return False, None
            self.remaining -= 1
        self.count += 1
        return True, np.full((1080, 1920, 3), self.count % 255, dtype=np.uint8)

    def release(self):
        self.released = True


def decode(jpeg):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def test_screenshot_sizes():
    capture = Capture("fake", opener=lambda device: Source())
    assert decode(capture.screenshot("fast")).shape == (540, 960, 3)
    assert decode(capture.screenshot("normal")).shape == (720, 1280, 3)
    assert decode(capture.screenshot("detail")).shape == (1080, 1920, 3)


def test_unknown_quality():
    capture = Capture("fake", opener=lambda device: Source())
    with pytest.raises(ValueError):
        capture.screenshot("ultra")


def test_device_released_when_idle_then_reopened():
    sources = []

    def opener(device):
        sources.append(Source())
        return sources[-1]

    capture = Capture("fake", idle_s=0.05, opener=opener)
    capture.screenshot()
    time.sleep(0.2)
    assert sources[0].released and not capture.running
    capture.screenshot()
    assert len(sources) == 2


def test_frame_is_newer_than_request():
    source = Source()
    capture = Capture("fake", opener=lambda device: source)
    capture.frame()
    seen = source.count
    time.sleep(0.05)
    capture.frame()
    assert source.count > seen


def test_reopens_after_signal_loss(monkeypatch):
    monkeypatch.setattr("switch_mcp.capture._REOPEN_DELAY_S", 0.01)
    monkeypatch.setattr("switch_mcp.capture._REOPEN_AFTER_FAILURES", 3)
    sources = []

    def opener(device):
        sources.append(Source(frames_before_failure=2 if not sources else None))
        return sources[-1]

    capture = Capture("fake", opener=opener)
    capture.screenshot()
    time.sleep(0.1)
    capture.screenshot()
    assert len(sources) == 2 and sources[0].released


def test_error_when_device_cannot_open(monkeypatch):
    monkeypatch.setattr("switch_mcp.capture.FIRST_FRAME_TIMEOUT_S", 0.1)
    monkeypatch.setattr("switch_mcp.capture._REOPEN_DELAY_S", 0.01)

    def opener(device):
        raise CaptureError("Cannot open fake")

    with pytest.raises(CaptureError, match="Cannot open fake"):
        Capture("fake", opener=opener).screenshot()
