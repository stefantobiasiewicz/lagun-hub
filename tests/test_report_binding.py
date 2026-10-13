"""A sensor's value on a display field: a binding report → write that carries the value
across (07-hub.md §3.1, §3.2; zadania/zrobione/FEAT-13.md).

The hub copies the report's bytes into the write — no conversion, "absent" included —
and writes on **every** report, periodic too, because the field forgets its content
after `ContentTimeout` while the temperature stands still.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.protocol.bus.virtual import VirtualBus
from lagunhub.protocol.endpoints.display import VALUE, Content
from lagunhub.protocol.endpoints.sense import VALUE as VALUE_SENSE
from lagunhub.protocol.registry import CommonAttribute, MessageKind, NodeClass
from lagunhub.protocol.values import ABSENT
from tests.conftest import Clock
from tests.node.devices import display2
from tests.node.endpoints.display import Display
from tests.node.endpoints.sense import Sense
from tests.node.node import NodeCore
from tests.test_api import Hub
from tests.test_display import heard

SENSOR_UID = bytes.fromhex("2021222324252627")
DISPLAY_UID = bytes.fromhex("1011121314151617")

CONFIG = f"""
buses: [vcan0]
nodes:
  "{SENSOR_UID.hex()}":
    alias: kitchen-env
    endpoints:
      4: {{ type: Sense }}
  "{DISPLAY_UID.hex()}":
    alias: hall-epd
    endpoints:
      4: {{ type: Display }}
bindings:
  - from: {{ node: kitchen-env, endpoint: 4, type: Sense,   attribute: Value }}
    to:   {{ node: hall-epd,    endpoint: 4, type: Display, attribute: Value }}
"""


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path, CONFIG)


@pytest.fixture
def screen(hub: Hub, clock: Clock) -> NodeCore:
    return hub.join(display2(uid=DISPLAY_UID, address=0, clock=clock, name="display"))


@pytest.fixture
def sensor(hub: Hub, clock: Clock, screen: NodeCore) -> NodeCore:
    del screen  # the field joins first, so the binding has a target from the start
    node = NodeCore(
        uid=SENSOR_UID,
        device_type=0x02F3,
        node_class=NodeClass.SENSOR,
        functional={4: Sense()},
        address=0,
        clock=clock,
        name="sensor",
    )
    return hub.join(node)


@pytest.fixture
def spy(hub: Hub) -> VirtualBus:
    return hub.inst.net.attach("spy")


def test_the_value_reaches_the_field(hub: Hub, screen: NodeCore, sensor: NodeCore) -> None:
    sensor.endpoint(4, Sense).measure(2137)  # 21.37 °C
    hub.inst.run(0.2)
    field = screen.endpoint(4, Display)
    assert (field.shows, field.values[VALUE]) == (Content.VALUE, 2137)


def test_a_periodic_report_writes_again(
    hub: Hub, screen: NodeCore, sensor: NodeCore, spy: VirtualBus
) -> None:
    """The same value twice is still a write: otherwise the field goes blank after
    `ContentTimeout` though the sensor is alive (zadania/zrobione/FEAT-13.md N8)."""
    sense = sensor.endpoint(4, Sense)
    sense.values[CommonAttribute.ReportInterval] = 10
    sense.measure(2137)
    hub.inst.run(0.2)
    heard(spy, MessageKind.WRITE)  # drain the first one

    hub.inst.run(10.5, step=0.05)
    writes = heard(spy, MessageKind.WRITE)
    assert len(writes) == 1, "one periodic report, one write"
    assert writes[0].data[1:] == (2137).to_bytes(2, "little", signed=True)


def test_absent_is_carried_across(hub: Hub, screen: NodeCore, sensor: NodeCore) -> None:
    """A sensor that lost its reading says so; the field shows dashes instead of a
    stale number (Sense §5.2; zadania/zrobione/FEAT-13.md N5)."""
    sense = sensor.endpoint(4, Sense)
    sense.measure(2137)
    hub.inst.run(0.2)
    assert screen.endpoint(4, Display).shows is Content.VALUE
    sense.set(VALUE_SENSE, ABSENT)  # `measure` takes a reading, not its absence
    hub.inst.run(0.2)
    assert screen.endpoint(4, Display).shows is Content.NONE


def test_the_view_says_which_attribute_is_reported(hub: Hub, sensor: NodeCore) -> None:
    """What the binding wizard in the view offers as a source (zadania/zrobione/FEAT-13.md)."""
    ep4 = next(e for e in hub.api.node("kitchen-env").detail if e.number == 4)
    reported = {a.name for a in ep4.attributes if a.reported}
    assert reported == {"Value"}


def test_the_hub_remembers_what_it_wrote(hub: Hub, screen: NodeCore, sensor: NodeCore) -> None:
    """A binding's write lands in the model as an API write does — otherwise the view shows
    the field empty while the screen shows the number."""
    sensor.endpoint(4, Sense).measure(2137)
    hub.inst.run(0.2)
    ep4 = next(e for e in hub.api.node("hall-epd").detail if e.number == 4)
    assert next(a for a in ep4.attributes if a.name == "Value").value == 2137
