"""`Sense` in units: the view shows a measurement in its quantity, "absent" as absent, and
the file and the API take thresholds and `Offset` in the quantity's units, converted
only once the node has said what it measures (zadania/zrobione/DEVICE-01.md N5, N12;
KONFIGURACJA.md §7.2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lagunhub.api import ApiError, Problem
from lagunhub.api.views import AttributeView
from lagunhub.core.config.loader import ConfigError
from lagunhub.core.events import EventKind
from lagunhub.protocol.endpoints.sense import (
    MAX_MEASURED,
    OFFSET,
    THRESHOLD,
    THRESHOLD_HIGH,
    THRESHOLD_HYSTERESIS,
    THRESHOLD_LOW,
    VALUE,
    Feature,
)
from lagunhub.protocol.quantities import Quantity
from lagunhub.protocol.registry import NodeClass
from lagunhub.protocol.values import ABSENT
from tests.conftest import Clock
from tests.node.endpoints.sense import Sense
from tests.node.node import NodeCore
from tests.test_api import Hub

UID = bytes.fromhex("2021222324252627")

CONFIG = f"""
buses: [vcan0]
nodes:
  "{UID.hex()}":
    alias: kitchen-env
    endpoints:
      4:
        type: Sense
        ThresholdHigh: 26.5
        ThresholdLow: 18
        ThresholdHysteresis: 0.5
        Offset: -0.5
      5: {{ type: Sense }}
      6:
        type: Sense
        ThresholdLow: 20
bindings: []
"""


def env(clock: Clock) -> NodeCore:
    """The board `env` as the hub sees it: a temperature, a humidity and a light, each
    with both features and the optional settings."""
    every = Feature.MIN_MAX | Feature.THRESHOLD
    optional = (OFFSET, THRESHOLD_HYSTERESIS)
    return NodeCore(
        uid=UID,
        device_type=0x0030,
        node_class=NodeClass.SENSOR,
        functional={
            4: Sense(features=every, optional=optional, quantity=Quantity.TEMPERATURE),
            5: Sense(features=every, optional=optional, quantity=Quantity.HUMIDITY),
            6: Sense(features=every, optional=optional, quantity=Quantity.ILLUMINANCE),
        },
        address=0,
        clock=clock,
        name="env",
    )


@pytest.fixture
def hub(clock: Clock, tmp_path: Path) -> Hub:
    return Hub(clock, tmp_path, CONFIG)


@pytest.fixture
def sensor(hub: Hub, clock: Clock) -> NodeCore:
    node = hub.join(env(clock))
    hub.inst.run(1.0, step=0.05)  # reconciliation writes what the file wants
    return node


def attribute(hub: Hub, endpoint: int, name: str) -> AttributeView:
    ep = next(e for e in hub.api.node("kitchen-env").detail if e.number == endpoint)
    return next(a for a in ep.attributes if a.name == name)


# --- the view (N12) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "raw", "label"),
    [(4, 2137, "21.37 °C"), (4, -520, "-5.20 °C"), (5, 4820, "48.20 %"), (6, 31200, "312.00 lx")],
)
def test_a_measurement_is_shown_in_its_units(
    hub: Hub, sensor: NodeCore, endpoint: int, raw: int, label: str
) -> None:
    sensor.endpoint(endpoint, Sense).measure(raw)
    hub.inst.run(0.2)
    seen = attribute(hub, endpoint, "Value")
    assert (seen.value, seen.label) == (raw, label)


def test_absent_is_shown_as_absent_never_as_zero(hub: Hub, sensor: NodeCore) -> None:
    """A failed reading is "I don't know" (types/0x0030-sense.md §5.2, §7) — no number and
    no unit, which the view draws as `—`."""
    sense = sensor.endpoint(4, Sense)
    sense.measure(2137)
    hub.inst.run(0.2)
    sense.set(VALUE, ABSENT)
    hub.inst.run(0.2)
    seen = attribute(hub, 4, "Value")
    assert (seen.value, seen.label) == (None, None)


def test_the_extremes_are_shown_in_units_too(hub: Hub, sensor: NodeCore) -> None:
    sense = sensor.endpoint(4, Sense)
    sense.values[MAX_MEASURED] = 3100
    hub.api.read("kitchen-env", 4, "MaxMeasured")
    hub.inst.run(0.2)
    assert attribute(hub, 4, "MaxMeasured").label == "31.00 °C"


# --- the file (N5) ------------------------------------------------------------------


def test_the_file_speaks_in_units_and_the_node_gets_its_own(sensor: NodeCore) -> None:
    sense = sensor.endpoint(4, Sense)
    assert sense.values[THRESHOLD_HIGH] == 2650
    assert sense.values[THRESHOLD_LOW] == 1800
    assert sense.values[THRESHOLD_HYSTERESIS] == 50
    assert sense.values[OFFSET] == -50, "Offset in °C too, though it travels as an i16"


def test_the_same_number_means_the_quantity_of_its_endpoint(sensor: NodeCore) -> None:
    """`20` is 20 lx on a light — four bytes, 2000 hundredths — not 20 °C."""
    assert sensor.endpoint(6, Sense).values[THRESHOLD_LOW] == 2000


def test_the_view_compares_the_file_with_the_node_in_units(hub: Hub, sensor: NodeCore) -> None:
    del sensor
    seen = attribute(hub, 4, "ThresholdHigh")
    assert (seen.wanted_label, seen.label, seen.matches) == ("26.50 °C", "26.50 °C", True)


def test_a_value_the_quantity_cannot_carry_is_an_issue_not_a_crash(
    clock: Clock, tmp_path: Path
) -> None:
    """Out of range is known only after enumeration — the second phase of validation
    (KONFIGURACJA.md §10): an issue, the rest of the node still configured."""
    hub = Hub(clock, tmp_path, CONFIG.replace("ThresholdHigh: 26.5", "ThresholdHigh: 400"))
    node = hub.join(env(clock))
    hub.inst.run(1.0, step=0.05)
    issues = hub.inst.installation.issues
    assert any("ThresholdHigh" in i and "400" in i for i in issues), issues
    sense = node.endpoint(4, Sense)
    assert sense.values[THRESHOLD_HIGH] is ABSENT
    assert sense.values[THRESHOLD_LOW] == 1800


def test_a_word_where_a_quantity_is_expected_stops_the_hub(clock: Clock, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ThresholdHigh"):
        Hub(clock, tmp_path, CONFIG.replace("ThresholdHigh: 26.5", "ThresholdHigh: warm"))


def test_the_api_writes_the_file_in_units(hub: Hub, sensor: NodeCore) -> None:
    hub.api.config_set_attribute(
        "kitchen-env", 4, "ThresholdHigh", "27.25", etag=hub.api.config().etag
    )
    hub.inst.run(1.0, step=0.05)
    assert "ThresholdHigh: 27.25" in hub.path.read_text()
    assert sensor.endpoint(4, Sense).values[THRESHOLD_HIGH] == 2725


# --- writes and events --------------------------------------------------------------


def test_a_write_is_in_units(hub: Hub, sensor: NodeCore) -> None:
    assert hub.api.write("kitchen-env", 4, "ThresholdLow", 17.5).ok
    assert sensor.endpoint(4, Sense).values[THRESHOLD_LOW] == 1750


def test_a_write_past_the_quantity_is_refused(hub: Hub, sensor: NodeCore) -> None:
    del sensor
    with pytest.raises(ApiError) as error:
        hub.api.write("kitchen-env", 4, "ThresholdLow", 400)
    assert error.value.problem is Problem.INVALID


def test_a_threshold_event_says_the_zone_and_the_value_in_units(hub: Hub, sensor: NodeCore) -> None:
    with hub.inst.core.events.subscribe() as events:
        zone, value = 2, (2601).to_bytes(2, "little", signed=True)  # ABOVE_HIGH, 26.01 °C
        sensor.endpoint(4, Sense).emit(THRESHOLD, bytes([zone]) + value)
        hub.inst.run(0.2)
        seen = list(iter(lambda: events.get(0), None))
    (event,) = [e for e in seen if e.kind is EventKind.EVENT]
    assert (event.name, event.text) == ("Threshold", "zone=ABOVE_HIGH value=26.01 °C")
