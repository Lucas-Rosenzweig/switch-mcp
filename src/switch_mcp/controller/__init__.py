from .base import BUTTONS, STICKS, Backend, PadState
from .pad import ControllerError, Pad

__all__ = ["BUTTONS", "STICKS", "Backend", "ControllerError", "Pad", "PadState", "make_backend"]


def make_backend(name: str) -> Backend:
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend()
    if name == "nuxbt":
        from .nuxbt_backend import NuxbtBackend

        return NuxbtBackend()
    raise ValueError(f"Unknown backend {name!r}; expected 'nuxbt' or 'fake'")
