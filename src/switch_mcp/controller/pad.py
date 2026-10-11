"""Persistent controller state with timed inputs and a release watchdog.

The pad owns the state reported to the console. Inputs can overlap (hold a
stick while pressing a button), and every held input carries a deadline so
nothing stays pressed if the caller disappears.
"""
from __future__ import annotations

import math
import threading
import time

from .base import BUTTONS, STICKS, Backend, PadState

MIN_PRESS_S = 0.02
MAX_PRESS_S = 5.0
MAX_SEQUENCE_STEPS = 64
MAX_SEQUENCE_S = 60.0
# Slack added to the deadline of a blocking action, so the watchdog only
# fires if the action itself failed to release.
_GRACE_S = 1.0


class ControllerError(RuntimeError):
    """The console cannot receive inputs right now."""


def normalize_buttons(buttons: list[str]) -> list[str]:
    if not isinstance(buttons, list) or not buttons:
        raise ValueError("buttons must be a non-empty list")
    names = [str(b).upper() for b in buttons]
    unknown = sorted(set(names) - BUTTONS)
    if unknown:
        raise ValueError(f"Unknown buttons {unknown}; valid: {sorted(BUTTONS)}")
    return names


def normalize_axis(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("stick axes must be numbers in [-1, 1]")
    if not -1.0 <= value <= 1.0:
        raise ValueError("stick axes must be in [-1, 1]")
    return float(value)


def normalize_side(side: str) -> str:
    if side not in STICKS:
        raise ValueError(f"side must be one of {STICKS}")
    return side


class Pad:
    def __init__(self, backend: Backend, default_hold_s: float = 5.0, max_hold_s: float = 30.0) -> None:
        self._backend = backend
        self._default_hold_s = default_hold_s
        self._max_hold_s = max_hold_s
        self._cond = threading.Condition()
        self._sequence_lock = threading.Lock()
        # Button name / stick side -> monotonic deadline.
        self._buttons: dict[str, float] = {}
        self._sticks: dict[str, tuple[float, float, float]] = {}
        self._applied = PadState()
        self._closed = False
        threading.Thread(target=self._watchdog, daemon=True, name="pad-watchdog").start()

    # -- state -------------------------------------------------------------

    def snapshot(self) -> PadState:
        with self._cond:
            return self._build()

    def _build(self) -> PadState:
        sticks = {side: (x, y) for side, (x, y, _) in self._sticks.items()}
        return PadState(
            buttons=frozenset(self._buttons),
            left=sticks.get("left", (0.0, 0.0)),
            right=sticks.get("right", (0.0, 0.0)),
        )

    def _push(self) -> None:
        """Send the current state to the backend. Caller holds the lock."""
        # Deadlines may have changed even if the state did not.
        self._cond.notify_all()
        state = self._build()
        if state == self._applied:
            return
        self._backend.apply(state)
        self._applied = state

    def _require_connected(self) -> None:
        state = self._backend.status().get("state")
        if state != "connected":
            raise ControllerError(f"Controller is {state}, not connected. Call switch_connect first.")

    def _hold_seconds(self, seconds: float | None) -> float:
        if seconds is None:
            return self._default_hold_s
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError("hold duration must be a positive number")
        return min(float(seconds), self._max_hold_s)

    # -- inputs ------------------------------------------------------------

    def hold(self, buttons: list[str], seconds: float | None = None) -> PadState:
        """Press buttons and keep them down until released or the deadline passes."""
        names = normalize_buttons(buttons)
        deadline = time.monotonic() + self._hold_seconds(seconds)
        with self._cond:
            self._require_connected()
            for name in names:
                self._buttons[name] = deadline
            self._push()
            return self._build()

    def release(self, buttons: list[str]) -> PadState:
        names = normalize_buttons(buttons)
        with self._cond:
            for name in names:
                self._buttons.pop(name, None)
            self._push()
            return self._build()

    def set_stick(self, side: str, x: float, y: float, seconds: float | None = None) -> PadState:
        """Tilt a stick until re-centred or the deadline passes. (0, 0) centres it."""
        side, x, y = normalize_side(side), normalize_axis(x), normalize_axis(y)
        deadline = time.monotonic() + self._hold_seconds(seconds)
        with self._cond:
            if (x, y) == (0.0, 0.0):
                self._sticks.pop(side, None)
            else:
                self._require_connected()
                self._sticks[side] = (x, y, deadline)
            self._push()
            return self._build()

    def press(self, buttons: list[str], duration_s: float = 0.1) -> PadState:
        """Press buttons, wait, release them. Other inputs stay as they are."""
        if not MIN_PRESS_S <= duration_s <= MAX_PRESS_S:
            raise ValueError(f"press duration must be in [{MIN_PRESS_S}, {MAX_PRESS_S}] seconds")
        self.hold(buttons, duration_s + _GRACE_S)
        try:
            time.sleep(duration_s)
        finally:
            state = self.release(buttons)
        return state

    def sequence(self, steps: list[dict]) -> float:
        """Play timed steps, each one a complete state, then release everything.

        A step is {"ms": int, "buttons": [...], "left": [x, y], "right": [x, y]};
        only "ms" is required, and a step with nothing else is a pause.
        Returns the total duration in seconds.
        """
        plan = self._plan(steps)
        if not self._sequence_lock.acquire(blocking=False):
            raise ControllerError("Another sequence is already running")
        try:
            for duration, buttons, sticks in plan:
                deadline = time.monotonic() + duration + _GRACE_S
                with self._cond:
                    self._require_connected()
                    self._buttons = {name: deadline for name in buttons}
                    self._sticks = {side: (x, y, deadline) for side, (x, y) in sticks.items()}
                    self._push()
                time.sleep(duration)
        finally:
            self.release_all()
            self._sequence_lock.release()
        return sum(step[0] for step in plan)

    @staticmethod
    def _plan(steps: list[dict]) -> list[tuple[float, list[str], dict[str, tuple[float, float]]]]:
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_SEQUENCE_STEPS:
            raise ValueError(f"steps must be a list of 1 to {MAX_SEQUENCE_STEPS} items")
        plan = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"step {index} must be an object")
            unknown = set(step) - {"ms", "buttons", "left", "right"}
            if unknown:
                raise ValueError(f"step {index} has unknown keys {sorted(unknown)}")
            ms = step.get("ms")
            if isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms < MIN_PRESS_S * 1000:
                raise ValueError(f"step {index} needs 'ms' >= {int(MIN_PRESS_S * 1000)}")
            buttons = normalize_buttons(step["buttons"]) if step.get("buttons") else []
            sticks = {}
            for side in STICKS:
                if step.get(side) is not None:
                    value = step[side]
                    if not isinstance(value, (list, tuple)) or len(value) != 2:
                        raise ValueError(f"step {index} '{side}' must be [x, y]")
                    position = (normalize_axis(value[0]), normalize_axis(value[1]))
                    if position != (0.0, 0.0):
                        sticks[side] = position
            plan.append((ms / 1000.0, buttons, sticks))
        total = sum(step[0] for step in plan)
        if total > MAX_SEQUENCE_S:
            raise ValueError(f"sequence lasts {total:.1f}s, maximum is {MAX_SEQUENCE_S:.0f}s")
        return plan

    def release_all(self) -> PadState:
        with self._cond:
            self._buttons.clear()
            self._sticks.clear()
            try:
                self._push()
            except Exception:
                # The link is gone, so nothing is held on the console side.
                self._applied = PadState()
            return self._build()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.release_all()

    # -- watchdog ----------------------------------------------------------

    def _watchdog(self) -> None:
        """Release inputs whose deadline passed."""
        with self._cond:
            while not self._closed:
                now = time.monotonic()
                deadlines = list(self._buttons.values()) + [s[2] for s in self._sticks.values()]
                due = [d for d in deadlines if d <= now]
                if due:
                    self._buttons = {n: d for n, d in self._buttons.items() if d > now}
                    self._sticks = {s: v for s, v in self._sticks.items() if v[2] > now}
                    try:
                        self._push()
                    except Exception:
                        self._applied = PadState()
                    continue
                self._cond.wait(timeout=min(deadlines) - now if deadlines else None)
