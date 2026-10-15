"""A knob dims a light: a binding event → command whose argument is a field of the event
(07-hub.md §3.2; zadania/devices/DEVICE-02.md N1, B2).

`ValueInput.Changed.value` goes into `Output.SetLevel.level` byte for byte — the scale is
the same ‰ on both sides, so there is nothing to convert. Offsets and types are settled
when the file is read; the fast path only copies. A newer command of such a binding
supersedes its older one still unanswered: a retried `SetLevel(300)` after
`SetLevel(500)` would move the light back.
"""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config.apply import apply
from lagunhub.core.config.loader import ConfigError, load
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import EndpointModel, Installation, NodeModel, NodeState, Uid
from lagunhub.core.rx import ColdItem, RxThread, Sent
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.endpoints.output import LEVEL, SET_LEVEL, STEP, Feature
from lagunhub.protocol.frame import Frame
from lagunhub.protocol.registry import EndpointType, MessageKind, NodeClass
from tests.conftest import Clock
from tests.node.devices import dial1
from tests.node.endpoints.output import Output
from tests.node.endpoints.value_input import ValueInput
from tests.node.node import NodeCore
from tests.test_display import heard
from tests.test_e2e import Rig

DIAL_UID = bytes.fromhex("3031323334353637")
DIMMER_UID = bytes.fromhex("4041424344454647")

NODES = f"""
buses: [vcan0]
nodes:
  "{DIAL_UID.hex()}":
    alias: hall-dial
    endpoints:
      4: {{ type: ValueInput }}
  "{DIMMER_UID.hex()}":
    alias: hall-light
    endpoints:
      4: {{ type: Output }}
"""

LEVEL_FROM_VALUE = (
    NODES
    + """bindings:
  - from: { node: hall-dial,  endpoint: 4, type: ValueInput, event: Changed }
    to:   { node: hall-light, endpoint: 4, type: Output, command: SetLevel,
            args: { level: $value, transition: 0 } }
"""
)


def rig(tmp_path: Path, *, dimmer_features: int | None = None) -> Rig:
    """`test_e2e.build` with a knob and a dimmer: the RX thread and the fast path, which
    the API's harness has not — an event binding lives only there."""
    path = tmp_path / "dial.yaml"
    path.write_text(LEVEL_FROM_VALUE, encoding="utf-8")
    clock = Clock()
    net = VirtualNetwork()
    dial = dial1(uid=DIAL_UID, address=0x02B, clock=clock, name="dial")
    light = NodeCore(
        uid=DIMMER_UID,
        device_type=0x02F4,
        node_class=NodeClass.CONTROL,
        functional={4: Output(channel=1, features=Feature.LEVEL)},
        address=0x041,
        clock=clock,
        name="dimmer",
    )
    dial.start(net.attach("dial"))
    light.start(net.attach("dimmer"))
    hub_port = net.attach("hub")
    installation = Installation()
    apply(installation, load(path))
    for uid, address in ((DIAL_UID, 0x02B), (DIMMER_UID, 0x041)):
        installation.add(NodeModel(uid=Uid(uid), address=address, bus=0, state=NodeState.READY))
    if dimmer_features is not None:
        # What enumeration would have read from the descriptor.
        installation.nodes[Uid(DIMMER_UID)].endpoints[4] = EndpointModel(
            4, EndpointType.Output, features=dimmer_features
        )
    cold: queue.Queue[ColdItem] = queue.Queue()
    engine = BindingEngine([hub_port], installation.snapshot())
    rx = RxThread([hub_port], engine, cold)
    engine.on_sent = lambda bus, frame, supersedes: rx.offer(
        Sent(bus, frame, clock.now, supersedes)
    )
    core = CoreThread(
        installation,
        cold,
        lambda s: setattr(engine, "snapshot", s),
        buses=[hub_port],
        clock=clock,
        tids=engine.tids,
    )
    preview = net.attach("spy")
    rx.pump()
    core.pump(clock.now)
    return Rig(clock, net, dial, light, rx, engine, core, preview)


def knob(r: Rig) -> ValueInput:
    return r.key_sim.endpoint(4, ValueInput)


def level(r: Rig) -> int:
    return int(r.relay.endpoint(4, Output).values[LEVEL])  # type: ignore[arg-type]


def commands(r: Rig) -> list[Frame]:
    return heard(r.preview, MessageKind.COMMAND)  # type: ignore[arg-type]


def test_turning_sets_the_level(tmp_path: Path) -> None:
    r = rig(tmp_path)
    knob(r).turn(3)  # 3 × 20 ‰
    r.run(0.2)
    assert level(r) == 60
    knob(r).turn(-1)
    r.run(0.2)
    assert level(r) == 40


def test_the_value_is_copied_and_the_constant_stays(tmp_path: Path) -> None:
    """`level` from the event, `transition` from the file — byte for byte."""
    r = rig(tmp_path)
    knob(r).turn(5)
    r.run(0.2)
    (command,) = commands(r)
    assert command.fields.element == SET_LEVEL
    assert command.data[1:] == (100).to_bytes(2, "little") + (0).to_bytes(2, "little")


def test_a_newer_value_supersedes_an_unanswered_one(tmp_path: Path) -> None:
    """The light does not answer the first command; the second comes before its retry.
    The first is not retried — it would arrive after the second and move the light back
    (zadania/devices/DEVICE-02.md B2)."""
    r = rig(tmp_path)
    r.relay.faults.drop_responses = 1
    knob(r).turn(10)  # 200 ‰, answered never
    r.run(0.05)
    knob(r).turn(5)  # 300 ‰, before the 100 ms retry of the first
    r.run(0.5)
    levels = [int.from_bytes(f.data[1:3], "little") for f in commands(r)]
    assert levels == [200, 300], "no retry of 200 after 300"
    assert level(r) == 300
    assert r.core.transactions.superseded == 1
    assert r.core.transactions.lost == 0


def test_an_unanswered_value_alone_is_retried(tmp_path: Path) -> None:
    """Nothing newer came: the lost command is retried as any other."""
    r = rig(tmp_path)
    r.relay.faults.drop_responses = 1
    knob(r).turn(10)
    r.run(0.5)
    assert [int.from_bytes(f.data[1:3], "little") for f in commands(r)] == [200, 200]
    assert level(r) == 200


def test_set_level_on_an_output_without_level_is_off(tmp_path: Path) -> None:
    """The descriptor says the output cannot dim: the binding sends nothing, and the
    problem is said (zadania/devices/DEVICE-02.md B3)."""
    r = rig(tmp_path, dimmer_features=0)
    knob(r).turn(3)
    r.run(0.2)
    assert commands(r) == []
    assert any("SetLevel needs a feature" in issue for issue in r.core.installation.issues)


def test_set_level_on_a_dimmer_is_on(tmp_path: Path) -> None:
    r = rig(tmp_path, dimmer_features=Feature.LEVEL)
    knob(r).turn(3)
    r.run(0.2)
    assert level(r) == 60


# --- reading the file ----------------------------------------------------------------


def config(tmp_path: Path, binding: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(NODES + "bindings:\n" + binding, encoding="utf-8")
    return path


def test_the_file_gives_the_offsets(tmp_path: Path) -> None:
    """`value` is byte 1 of the event (after the sequence number), `level` byte 1 of the
    command (after the header); both `u16`."""
    loaded = load(config(tmp_path, LEVEL_FROM_VALUE.split("bindings:\n")[1]))
    (target,) = loaded.bindings[0].targets
    assert target.splices == ((1, 1, 2),)


def test_delta_goes_into_step(tmp_path: Path) -> None:
    """The same mechanism, `i16` into `i16`: `Changed.delta` at byte 3 of the event."""
    loaded = load(
        config(
            tmp_path,
            """  - from: { node: hall-dial,  endpoint: 4, type: ValueInput, event: Changed }
    to:   { node: hall-light, endpoint: 4, type: Output, command: Step,
            args: { delta: $delta, transition: 0 } }
""",
        )
    )
    (target,) = loaded.bindings[0].targets
    assert (target.element, target.splices) == (STEP, ((3, 1, 2),))


@pytest.mark.parametrize(
    ("binding", "says"),
    [
        (
            """  - from: { node: hall-dial,  endpoint: 4, type: ValueInput, attribute: Value }
    to:   { node: hall-light, endpoint: 4, type: Output, command: SetLevel,
            args: { level: $value, transition: 0 } }
""",
            "needs an event",
        ),
        (
            """  - from: { node: hall-dial,  endpoint: 4, type: ValueInput, event: Changed }
    to:   { node: hall-light, endpoint: 4, type: Output, command: SetLevel,
            args: { level: $speed, transition: 0 } }
""",
            "no field 'speed'",
        ),
        (
            """  - from: { node: hall-dial,  endpoint: 4, type: ValueInput, event: Changed }
    to:   { node: hall-light, endpoint: 4, type: Output, command: SetLevel,
            args: { level: $delta, transition: 0 } }
""",
            "converts nothing",
        ),
    ],
    ids=["a report as the source", "an unknown field", "i16 into u16"],
)
def test_the_file_is_refused(tmp_path: Path, binding: str, says: str) -> None:
    with pytest.raises(ConfigError, match=says):
        load(config(tmp_path, binding))
