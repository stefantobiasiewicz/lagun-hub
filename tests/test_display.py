"""`Display` end to end: the API writes a number and a text, the text goes as a
LONG_WRITE, the view shows what the field shows (types/0x0022-display.md; FEAT-12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.api import ApiError, Problem
from lagunhub.api.views import AttributeView
from lagunhub.protocol.bus.virtual import VirtualBus
from lagunhub.protocol.endpoints.display import Content
from lagunhub.protocol.frame import Frame
from lagunhub.protocol.registry import MessageKind
from tests.conftest import Clock
from tests.node.devices import display2
from tests.node.endpoints.display import Display
from tests.node.node import NodeCore
from tests.test_api import Hub

UID = bytes.fromhex("1011121314151617")

CONFIG = f"""
buses: [vcan0]
nodes:
  "{UID.hex()}":
    alias: kitchen-display
    endpoints:
      4: {{ type: Display }}
      5: {{ type: Display }}
bindings: []
"""


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path, CONFIG)


@pytest.fixture
def screen(hub: Hub, clock: Clock) -> NodeCore:
    return hub.join(display2(uid=UID, address=0, clock=clock, name="display"))


@pytest.fixture
def spy(hub: Hub) -> VirtualBus:
    """A third port on the cable: it hears what the hub sends."""
    return hub.inst.net.attach("spy")


def attribute(hub: Hub, endpoint: int, name: str) -> AttributeView:
    ep = next(e for e in hub.api.node("kitchen-display").detail if e.number == endpoint)
    return next(a for a in ep.attributes if a.name == name)


def heard(spy: VirtualBus, kind: MessageKind) -> list[Frame]:
    frames = []
    while (frame := spy.recv()) is not None:
        if not frame.is_join and frame.fields.kind == kind:
            frames.append(frame)
    return frames


def test_a_number_is_a_write_and_the_view_reads_it_in_its_quantity(
    hub: Hub, screen: NodeCore
) -> None:
    # In the quantity's units, as the view shows it (zadania/zrobione/DEVICE-01.md N12).
    result = hub.api.write("kitchen-display", 4, "Value", 21.37)
    assert result.ok
    field = screen.endpoint(4, Display)
    assert (field.shows, field.values[0x02]) == (Content.VALUE, 2137)
    hub.inst.run(0.1, step=0.01)
    seen = attribute(hub, 4, "Value")
    assert (seen.value, seen.label) == (2137, "21.37 °C"), "<Q> decoded by Quantity"
    assert attribute(hub, 4, "Content").label == "VALUE", "Content was reported"


def test_a_text_goes_as_a_long_write_and_comes_back_as_text(
    hub: Hub, screen: NodeCore, spy: VirtualBus
) -> None:
    assert hub.api.write("kitchen-display", 5, "Text", "Pranie gotowe").ok
    frames = [f for f in iter(spy.recv, None) if not f.is_join]
    assert [f.fields.kind for f in frames].count(MessageKind.WRITE) == 0, "never a WRITE"
    long_writes = [f for f in frames if f.fields.kind == MessageKind.LONG_WRITE]
    assert len(long_writes) == 3, "PRZYKLADY 10: three segments"
    assert screen.endpoint(5, Display).shown_text == "Pranie gotowe"
    assert attribute(hub, 5, "Text").value == "Pranie gotowe"

    assert hub.api.read("kitchen-display", 5, "Text").ok, "read back in segments"
    assert attribute(hub, 5, "Text").value == "Pranie gotowe"


def test_a_text_longer_than_the_field_is_refused_before_the_bus(
    hub: Hub, screen: NodeCore, spy: VirtualBus
) -> None:
    """`TextLength` counts bytes: sixteen letters, seventeen bytes. Read at enumeration,
    so the hub need not ask."""
    with pytest.raises(ApiError) as error:
        hub.api.write("kitchen-display", 5, "Text", "Pranie skończone")
    assert error.value.problem is Problem.INVALID
    assert "17 bytes" in str(error.value)
    assert heard(spy, MessageKind.LONG_WRITE) == []


def test_a_lost_segment_repeats_the_whole_text(hub: Hub, screen: NodeCore, spy: VirtualBus) -> None:
    """The node drops a value with a gap and says nothing; the hub's retry sends it all
    again (05-mapowanie-can.md §6.6)."""
    original = screen._take_segment
    dropped: list[int] = []

    def lossy(number: int, element: int, data: bytes) -> None:
        if data[1] == 1 and not dropped:
            dropped.append(1)
            return
        original(number, element, data)

    screen._take_segment = lossy  # type: ignore[method-assign]
    result = hub.api.write("kitchen-display", 5, "Text", "Pranie gotowe")
    assert result.ok and result.attempts == 2
    assert len(heard(spy, MessageKind.LONG_WRITE)) == 6
    assert screen.endpoint(5, Display).shown_text == "Pranie gotowe"


def test_content_expires_and_the_hub_hears_it(hub: Hub, screen: NodeCore) -> None:
    hub.api.write("kitchen-display", 5, "Text", "wietrz")
    hub.inst.run(121.0, step=1.0)
    assert screen.endpoint(5, Display).shows is Content.NONE
    assert attribute(hub, 5, "Content").label == "NONE"
