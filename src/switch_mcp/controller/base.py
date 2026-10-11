"""Controller state model and the interface every backend implements."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

BUTTONS = frozenset({
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "HOME", "CAPTURE",
    "UP", "DOWN", "LEFT", "RIGHT", "L_STICK", "R_STICK",
})
STICKS = ("left", "right")


@dataclass(frozen=True)
class PadState:
    """Everything the emulated controller reports at one instant."""

    buttons: frozenset[str] = field(default_factory=frozenset)
    # Stick positions in [-1, 1]; +x is right, +y is up.
    left: tuple[float, float] = (0.0, 0.0)
    right: tuple[float, float] = (0.0, 0.0)

    @property
    def idle(self) -> bool:
        return not self.buttons and self.left == (0.0, 0.0) and self.right == (0.0, 0.0)


class Backend(Protocol):
    """Transport that makes the console see a PadState."""

    def connect(self) -> None:
        """Start pairing or reconnecting. Must not block until connected.
        Calling it while a connection attempt is pending restarts the attempt."""

    def disconnect(self) -> None:
        """Drop the link to the console; connect() can be called again."""

    def status(self) -> dict:
        """Return at least {'state': ...}; inputs are accepted only when it is 'connected'."""

    def apply(self, state: PadState) -> None:
        """Make `state` the reported state until the next call."""

    def close(self) -> None:
        """Release everything and free the transport."""
