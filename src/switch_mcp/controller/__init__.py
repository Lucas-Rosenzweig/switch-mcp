from .base import BUTTONS, STICKS, Backend, PadState
from .pad import ControllerError, Pad

__all__ = ["BUTTONS", "STICKS", "Backend", "ControllerError", "Pad", "PadState", "make_backend"]


def make_backend(name: str, settle_s: float | None = None, switch_address: str | None = None) -> Backend:
    """`settle_s` and `switch_address` only concern the Bluetooth backend."""
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend()
    if name == "nuxbt":
        from .nuxbt_backend import DEFAULT_SETTLE_S, NuxbtBackend

        return NuxbtBackend(DEFAULT_SETTLE_S if settle_s is None else settle_s, switch_address)
    raise ValueError(f"Unknown backend {name!r}; expected 'nuxbt' or 'fake'")
