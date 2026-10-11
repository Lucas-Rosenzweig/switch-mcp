"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    # "nuxbt" drives a real Switch over Bluetooth; "fake" records inputs only.
    backend: str = "nuxbt"
    video_device: str = "/dev/video0"
    # Seconds without a screenshot before the capture device is released.
    capture_idle_s: float = 30.0
    # A held input is released automatically after this long without renewal.
    default_hold_s: float = 5.0
    max_hold_s: float = 30.0
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            backend=os.environ.get("SWITCH_MCP_BACKEND", cls.backend),
            video_device=os.environ.get("SWITCH_MCP_VIDEO_DEVICE", cls.video_device),
            capture_idle_s=_float("SWITCH_MCP_CAPTURE_IDLE_S", cls.capture_idle_s),
            default_hold_s=_float("SWITCH_MCP_DEFAULT_HOLD_S", cls.default_hold_s),
            max_hold_s=_float("SWITCH_MCP_MAX_HOLD_S", cls.max_hold_s),
            host=os.environ.get("SWITCH_MCP_HOST", cls.host),
            port=int(os.environ.get("SWITCH_MCP_PORT", cls.port)),
        )
