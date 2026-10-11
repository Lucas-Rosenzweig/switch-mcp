"""In-memory backend for tests and for trying the MCP tools without hardware."""
from __future__ import annotations

import time

from .base import PadState


class FakeBackend:
    def __init__(self) -> None:
        self.state = "idle"
        self.current = PadState()
        # (monotonic time, state) for every apply(), oldest first.
        self.history: list[tuple[float, PadState]] = []

    def connect(self) -> None:
        self.state = "connected"

    def status(self) -> dict:
        return {"state": self.state, "backend": "fake"}

    def apply(self, state: PadState) -> None:
        self.current = state
        self.history.append((time.monotonic(), state))

    def disconnect(self) -> None:
        self.state = "idle"
        self.current = PadState()

    def close(self) -> None:
        self.state = "idle"
        self.current = PadState()
