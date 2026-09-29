"""The behaviour of the KeyInput type — a key's state machine.

Definitions of attributes and events: `lagunhub.protocol.endpoints.key_input`. Here there
is only what cannot be in a catalogue: **when** which event comes about. This machine is
the source of most mistakes in firmware.

The double has **two entrances**: ``press()``/``release()`` go through the state machine
and check the type, while ``force_click()`` goes around it and serves for testing the hub.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import ClassVar

from lagunhub.protocol.endpoints.definition import TypeDef
from lagunhub.protocol.endpoints.key_input import (
    CLICK,
    DOWN,
    EVENT_MODE,
    HOLD_ENABLED,
    HOLD_END,
    HOLD_START,
    HOLD_THRESHOLD,
    HOLD_TICK,
    HOLD_TICK_INTERVAL,
    HOLD_TICK_LIMIT_S,
    KEY_INPUT,
    MAX_CLICKS,
    MULTI_CLICK_WINDOW,
    PRESS_COUNT,
    UP,
    EventMode,
    Feature,
)
from lagunhub.protocol.values import Value, encode
from tests.node.endpoints.base import FunctionalEndpoint


class _State(Enum):
    RELEASED = auto()
    BOUNCING = auto()
    PRESSED = auto()
    HELD = auto()
    COUNTING = auto()  # waiting to see whether another press arrives (MULTICLICK)


DEBOUNCE_S = 0.015
"""The double's own contact bounce. Not an attribute: bounce belongs to how a device is
built, and the type leaves it to the device (types/0x0010-key-input.md §5.7)."""


class KeyInput(FunctionalEndpoint):
    """A key. What it can do comes from ``features``; which timings the hub may tune
    comes from ``optional`` — the rest are the double's own, at the type's
    recommended values (types/0x0010-key-input.md §2.4)."""

    TYPE: ClassVar[TypeDef] = KEY_INPUT

    def __post_init__(self) -> None:
        super().__post_init__()
        self._state = _State.RELEASED
        self._changed_at = 0.0
        self._pressed_at = 0.0
        self._next_tick = 0.0
        self._clicks = 0
        self._deadline = 0.0

    def _setting(self, attribute: int) -> int:
        """The attribute when the device exposes it, its own value when not."""
        default = KEY_INPUT.attributes[attribute].default
        assert isinstance(default, int), "every KeyInput timing has a numeric default"
        return self.opt(attribute, default)

    def _ms(self, attribute: int) -> float:
        return self._setting(attribute) / 1000.0

    def _hold(self) -> bool:
        return self.has(Feature.HOLD) and bool(self.values[HOLD_ENABLED])

    # --- the physical input -------------------------------------------------

    def press(self, now: float) -> None:
        """The contact is closed. The event comes about only after the bounce."""
        if self._state in (_State.RELEASED, _State.COUNTING):
            self._state = _State.BOUNCING
            self._changed_at = now

    def release(self, now: float) -> None:
        """The contact is open."""
        if self._state == _State.BOUNCING:
            self._state = _State.RELEASED  # a bounce shorter than the debounce
            return
        if self._state == _State.HELD:
            self.emit(HOLD_END, self._elapsed(now))
            self._state = _State.RELEASED
            return
        if self._state != _State.PRESSED:
            return

        mode = self.num(EVENT_MODE)
        if mode == EventMode.RAW:
            self.emit(UP, self._elapsed(now))
            self._state = _State.RELEASED
        elif mode == EventMode.MULTICLICK:
            self._clicks += 1
            if self._clicks >= self._setting(MAX_CLICKS):
                self._flush_clicks()
            else:
                self._state = _State.COUNTING
                self._deadline = now + self._ms(MULTI_CLICK_WINDOW)
        else:
            # A Click comes about on RELEASE, not on the press (§5.2).
            self.emit(CLICK, encode("u8", 1))
            self._state = _State.RELEASED

    def force_click(self, count: int = 1) -> None:
        """Send a Click around the state machine — for testing the hub."""
        self.emit(CLICK, encode("u8", count))

    def _elapsed(self, now: float) -> bytes:
        return encode("u16", min(int((now - self._pressed_at) * 1000), 65534))

    # --- the clock ----------------------------------------------------------

    def tick(self, now: float) -> None:
        if self._state == _State.BOUNCING and now - self._changed_at >= DEBOUNCE_S:
            self._state = _State.PRESSED
            self._pressed_at = now
            self.values[PRESS_COUNT] = self.num(PRESS_COUNT) + 1
            if self.num(EVENT_MODE) == EventMode.RAW:
                self.emit(DOWN)
            return

        if self._state == _State.PRESSED and self._hold():
            if now - self._pressed_at >= self._ms(HOLD_THRESHOLD):
                # A press longer than the threshold turns into a hold and gives
                # NO Click at all (§5.3).
                self._state = _State.HELD
                self._clicks = 0
                self.emit(HOLD_START)
                self._next_tick = now + self._ms(HOLD_TICK_INTERVAL)
            return

        if (
            self._state == _State.HELD
            and now >= self._next_tick
            and now - self._pressed_at < HOLD_TICK_LIMIT_S
        ):
            # Past the limit the key is taken to be stuck, and it may not keep the bus
            # busy; HoldEnd still comes on release (§5.6).
            self.emit(HOLD_TICK, self._elapsed(now))
            self._next_tick += self._ms(HOLD_TICK_INTERVAL)
            return

        if self._state == _State.COUNTING and now >= self._deadline:
            self._flush_clicks()

    def _flush_clicks(self) -> None:
        if self._clicks:
            self.emit(CLICK, encode("u8", self._clicks))
        self._clicks = 0
        self._state = _State.RELEASED

    def on_write(self, attribute: int, value: Value) -> None:
        if attribute == EVENT_MODE:
            self._clicks = 0
            self._state = _State.RELEASED
