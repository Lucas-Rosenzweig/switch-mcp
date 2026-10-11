"""On-demand HDMI capture.

The device is opened at the first screenshot, kept warm while screenshots
keep coming, and released after an idle period so it costs nothing between
sessions and stays available to other programs. A lost signal is handled by
reopening the device.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import cv2
import numpy as np

QUALITIES = {
    # name: (max width or None for native, JPEG quality)
    "fast": (960, 65),
    "normal": (1280, 80),
    "detail": (None, 90),
}
PIXEL_FORMAT = "BGR3"
FIRST_FRAME_TIMEOUT_S = 5.0
_REOPEN_AFTER_FAILURES = 30
_REOPEN_DELAY_S = 1.0


class CaptureError(RuntimeError):
    pass


class FrameSource(Protocol):
    def read(self) -> tuple[bool, np.ndarray | None]: ...
    def release(self) -> None: ...


def _open(device: str) -> cv2.VideoCapture:
    source = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not source.isOpened():
        source.release()
        raise CaptureError(f"Cannot open {device}; is another program using the capture card?")
    return source


def _fourcc(source: cv2.VideoCapture) -> str:
    value = int(source.get(cv2.CAP_PROP_FOURCC))
    return "".join(chr(value >> 8 * i & 0xFF) for i in range(4))


def open_v4l2(device: str) -> FrameSource:
    # Two quirks of the sc0710 driver, both observed on this card:
    # - its default YUYV output comes out of OpenCV with wrong colours, so BGR
    #   is requested (the setting persists on the device);
    # - the first stream started after the HDMI signal changed (console docked,
    #   woken up...) delivers unconverted YCbCr, shown as a teal picture with
    #   pink text; stopping and restarting the stream fixes it.
    # Hence a short throwaway stream before the real one.
    warmup = _open(device)
    if _fourcc(warmup) != PIXEL_FORMAT:
        warmup.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*PIXEL_FORMAT))
    warmup.read()
    warmup.release()
    source = _open(device)
    # Keep only the newest frame so a screenshot is never a stale one.
    source.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return source


class Capture:
    def __init__(
        self,
        device: str,
        idle_s: float = 30.0,
        opener: Callable[[str], FrameSource] = open_v4l2,
    ) -> None:
        self._device = device
        self._idle_s = idle_s
        self._opener = opener
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._frame_time = 0.0
        self._last_request = 0.0
        self._error: str | None = None
        self._running = False

    @property
    def running(self) -> bool:
        with self._cond:
            return self._running

    def status(self) -> dict:
        """What an agent needs to know about the picture, without opening the device."""
        with self._cond:
            fresh = self._frame is not None and time.monotonic() - self._frame_time < 1.0
            # Unknown while the device is closed: only a screenshot opens it.
            signal = fresh if self._running or self._error else None
            return {"capture_open": self._running, "hdmi_signal": signal, "capture_error": self._error}

    def _reader(self) -> None:
        source = None
        failures = 0
        try:
            while True:
                with self._cond:
                    if time.monotonic() - self._last_request > self._idle_s:
                        # Decided under the lock, so a concurrent frame() call
                        # either renews the deadline first or starts a new reader.
                        self._running = False
                        self._frame = None
                        return
                if source is None:
                    try:
                        source = self._opener(self._device)
                        failures = 0
                    except CaptureError as exc:
                        self._set_error(str(exc))
                        time.sleep(_REOPEN_DELAY_S)
                        continue
                ok, frame = source.read()
                if not ok or frame is None or frame.ndim != 3:
                    failures += 1
                    if failures >= _REOPEN_AFTER_FAILURES:
                        # Typically an HDMI signal loss or a resolution change.
                        self._set_error("No frames from the capture card; reopening")
                        source.release()
                        source = None
                        time.sleep(_REOPEN_DELAY_S)
                    else:
                        time.sleep(0.01)
                    continue
                failures = 0
                with self._cond:
                    self._frame = frame
                    self._frame_time = time.monotonic()
                    self._error = None
                    self._cond.notify_all()
        except Exception as exc:
            with self._cond:
                self._running = False
                self._frame = None
                self._error = f"Capture failed: {exc}"
        finally:
            if source is not None:
                source.release()

    def _set_error(self, message: str) -> None:
        with self._cond:
            self._error = message

    def frame(self) -> np.ndarray:
        """Return a frame captured after this call was made."""
        requested = time.monotonic()
        with self._cond:
            self._last_request = requested
            if not self._running:
                self._running = True
                threading.Thread(target=self._reader, daemon=True, name="capture").start()
            fresh = self._cond.wait_for(
                lambda: self._frame is not None and self._frame_time >= requested,
                timeout=FIRST_FRAME_TIMEOUT_S,
            )
            if not fresh:
                raise CaptureError(self._error or "No frame from the capture card (is the Switch docked and on?)")
            return self._frame

    def screenshot(self, quality: str = "normal", crop: tuple[float, float, float, float] | None = None) -> bytes:
        """JPEG of a fresh frame. `crop` is (x, y, width, height) as fractions of the
        picture; the cropped area is then scaled like a full frame, which enlarges it."""
        if quality not in QUALITIES:
            raise ValueError(f"quality must be one of {sorted(QUALITIES)}")
        box = _crop_box(crop) if crop is not None else None
        frame = self.frame()
        if box is not None:
            h, w = frame.shape[:2]
            x0, y0, x1, y1 = (round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h))
            frame = frame[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)]
        width, jpeg_quality = QUALITIES[quality]
        if box is not None and width is not None:
            # Enlarge a small region so details are legible, up to the quality's width.
            height = round(frame.shape[0] * width / frame.shape[1])
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_CUBIC)
        elif width is not None and frame.shape[1] > width:
            height = round(frame.shape[0] * width / frame.shape[1])
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        if not ok:
            raise CaptureError("JPEG encoding failed")
        return encoded.tobytes()


def _crop_box(crop) -> tuple[float, float, float, float]:
    """Validate (x, y, width, height) fractions and return (x0, y0, x1, y1)."""
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise ValueError("crop must be [x, y, width, height], fractions of the picture")
    x, y, w, h = (float(v) for v in crop)
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        raise ValueError("crop values must be fractions: x and y in [0, 1), width and height in (0, 1]")
    if x + w > 1.0001 or y + h > 1.0001:
        raise ValueError("crop must stay inside the picture: x + width <= 1 and y + height <= 1")
    return x, y, min(x + w, 1.0), min(y + h, 1.0)
