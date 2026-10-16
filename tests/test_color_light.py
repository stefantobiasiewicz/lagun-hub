"""An `Output` with `LEVEL | COLOR_RGB` through the hub — the colour light of
zadania/devices/DEVICE-03.md (`rgb8`, eight WS2812 as one endpoint).

The hub knew the type in full before; what is new is the colour's contract
(types/0x0020-output.md §5.8): the colour is independent of the level, persistent, white
after a factory reset, and black is not a colour. The hub has to carry `SetColor` with its
arguments, take the reports into the model, and show the light in one piece in the view.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.adapters.tui import panes
from lagunhub.api import ApiError, AttributeView, EndpointView
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.registry import NodeClass
from tests.conftest import Clock
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_api import Hub

LIGHT_UID = bytes(range(16, 24))

CONFIG = """
buses: [vcan0]
nodes:
  "1011121314151617":
    alias: shelf-light
    name: "The shelf"
    room: living-room
    endpoints:
      4: { type: Output, Purpose: LIGHT }
bindings: []
"""


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path, CONFIG)


@pytest.fixture
def light(hub: Hub, clock: Clock) -> NodeCore:
    node = NodeCore(
        uid=LIGHT_UID,
        device_type=0x0022,
        node_class=NodeClass.CONTROL,
        functional={4: Output(channel=1, features=out.Feature.LEVEL | out.Feature.COLOR_RGB)},
        address=0,
        clock=clock,
        name="rgb8",
    )
    return hub.join(node)


def endpoint(hub: Hub) -> EndpointView:
    return next(e for e in hub.api.node("shelf-light").detail if e.number == 4)


def known(hub: Hub) -> None:
    """Read the light's state once, as `G` in the view does. A report carries only what
    changed, so a component a `SetColor` left alone is otherwise not known to the hub."""
    for name in ("OnOff", "Level", "Red", "Green", "Blue", "White"):
        hub.api.read("shelf-light", 4, name)


def value(hub: Hub, name: str) -> object:
    return next(a.value for a in endpoint(hub).attributes if a.name == name)


class TestTheColour:
    def test_white_after_a_factory_reset(self, light: NodeCore) -> None:
        """Not black: `On` after a power cut must light something (§5.8)."""
        lamp = light.endpoint(4, Output)
        assert [lamp.values[a] for a in (out.RED, out.GREEN, out.BLUE, out.WHITE)] == [
            254,
            254,
            254,
            0,
        ], "0xFF is u8's 'absent', so full is 254"

    def test_set_color_reaches_the_node_and_comes_back_as_reports(
        self, hub: Hub, light: NodeCore
    ) -> None:
        known(hub)
        hub.api.command("shelf-light", 4, "SetLevel", {"level": 500, "transition": 0})
        result = hub.api.command(
            "shelf-light", 4, "SetColor", {"r": 0, "g": 254, "b": 0, "w": 0, "transition": 0}
        )
        assert result.ok
        hub.inst.run(0.1)
        assert (value(hub, "Red"), value(hub, "Green"), value(hub, "Blue")) == (0, 254, 0)
        assert value(hub, "Level") == 500, "the colour does not move the level"

    def test_black_is_refused_by_the_node(self, hub: Hub, light: NodeCore) -> None:
        with pytest.raises(ApiError, match="INVALID_VALUE"):
            hub.api.command(
                "shelf-light", 4, "SetColor", {"r": 0, "g": 0, "b": 0, "w": 0, "transition": 0}
            )

    def test_255_is_not_a_component(self, hub: Hub, light: NodeCore) -> None:
        """0xFF on the wire is "absent" — never sent as a colour, whoever refuses it."""
        with pytest.raises(ApiError):
            hub.api.command(
                "shelf-light", 4, "SetColor", {"r": 255, "g": 0, "b": 0, "w": 0, "transition": 0}
            )
        assert light.endpoint(4, Output).values[out.RED] == 254

    def test_set_color_does_not_light_a_dark_lamp(self, hub: Hub, light: NodeCore) -> None:
        hub.api.command(
            "shelf-light", 4, "SetColor", {"r": 254, "g": 0, "b": 0, "w": 0, "transition": 0}
        )
        lamp = light.endpoint(4, Output)
        assert lamp.values[out.ON_OFF] is False and lamp.values[out.LEVEL] == 0

    def test_on_comes_back_at_last_level(self, hub: Hub, light: NodeCore) -> None:
        hub.api.command("shelf-light", 4, "SetLevel", {"level": 300, "transition": 0})
        hub.api.command("shelf-light", 4, "Off")
        hub.api.command("shelf-light", 4, "On")
        assert light.endpoint(4, Output).values[out.LEVEL] == 300


class TestTheView:
    def test_one_line_says_on_how_bright_and_what_colour(self, hub: Hub, light: NodeCore) -> None:
        known(hub)
        hub.api.command("shelf-light", 4, "SetLevel", {"level": 625, "transition": 0})
        hub.api.command(
            "shelf-light", 4, "SetColor", {"r": 254, "g": 128, "b": 0, "w": 0, "transition": 0}
        )
        hub.inst.run(0.1)
        assert panes._summary(endpoint(hub)) == "on · 62.5 % · #FE8000"

    def test_dark_keeps_its_colour_in_the_line(self, hub: Hub, light: NodeCore) -> None:
        known(hub)
        assert panes._summary(endpoint(hub)) == "off · 0 % · #FEFEFE"

    def test_a_relay_still_says_on_off(self) -> None:
        relay = EndpointView(
            number=4,
            type_name="Output",
            attributes=(
                AttributeView(id=out.ON_OFF, name="OnOff", type="bool", access="RO", value=True),
            ),
        )
        assert panes._summary(relay) == "OnOff = yes"
