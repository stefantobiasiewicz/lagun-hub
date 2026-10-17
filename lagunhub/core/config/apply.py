"""Carrying the configuration over into the model.

The configuration describes **decisions**, the model describes the **installation**.
They meet here and only here — which is why the model need know nothing about the file
format.
"""

from __future__ import annotations

from collections.abc import Sequence

from lagunhub.core.config.model import BindingConfig, Config, NodeConfig, SourceRef, TargetRef
from lagunhub.core.model import BindingSpec, Installation, NodeModel, NodeState, Source, Uid


def apply(installation: Installation, config: Config, buses: Sequence[str] | None = None) -> None:
    """Load the configuration into the model. Only CORE calls this.

    `buses` are the names of the buses the hub has open, in order — what a `joined.bus`
    from the file is resolved against. `None`: the buses from the file, as `run` opens
    them without `--bus` or `--udp`.
    """
    installation.wanted = {entry.uid: entry for entry in config.nodes}
    installation.blocked = frozenset(entry.uid for entry in config.nodes if entry.blocked)
    installation.bindings = [spec for binding in config.bindings for spec in _specs(binding)]
    for uid, node in installation.nodes.items():
        wanted = config.node(uid)
        if wanted is not None and wanted.name:
            node.name = wanted.name
    opened = [bus.channel for bus in config.buses] if buses is None else list(buses)
    for entry in config.nodes:
        if entry.joined is not None and entry.uid not in installation.nodes:
            _restore(installation, config, opened, entry)


def _restore(
    installation: Installation, config: Config, opened: Sequence[str], entry: NodeConfig
) -> None:
    """A node the file says is joined: in the model at once, under its address
    (zadania/zrobione/BUG-05.md, D3).

    `READY` — the hub talks to it straight away, and bindings with it as a target work
    from the first second. Not "not heard since the start", not polled: a node that is
    in the file is joined. What the hub does not know yet is its descriptor
    (`described=False`); that is read at its first frame.

    Only for a node the model does not hold. One it holds, the hub learned from the bus
    — that wins over a file, which may be older, and the two disagreeing is reported
    when the hub writes its own entry back (`CoreThread.note_joined`).
    """
    joined = entry.joined
    assert joined is not None
    bus = bus_index(config, opened, joined.bus)
    if bus is None:
        # Not a guess at one of several buses: an address on the wrong bus is a
        # conflict only the hub could see (03-adresacja.md §6). The node is found the
        # ordinary way instead — its first frame, its `Uid` in the file
        # (06-cykl-zycia-urzadzenia.md §2.3).
        _issue(
            installation,
            f"{entry.alias}: bus {joined.bus!r} from the file is not open — the hub will "
            f"find the node at its first frame",
        )
        return
    holder = installation.by_address.get(joined.address)
    record = installation.records.get(joined.short_id)
    if (holder is not None and holder != entry.uid) or (
        record is not None and record.uid not in (None, entry.uid)
    ):
        _issue(
            installation,
            f"{entry.alias}: the file puts it at 0x{joined.address:03X} with ShortId "
            f"0x{joined.short_id:08X}, which another node holds — the file and the bus "
            f"disagree",
        )
        return
    state = NodeState.BLOCKED if entry.blocked else NodeState.READY
    installation.add(
        NodeModel(
            uid=entry.uid,
            address=joined.address,
            bus=bus,
            state=state,
            short_id=joined.short_id,
            name=entry.name,
            described=False,
        )
    )
    record = installation.record(joined.short_id)
    record.address, record.bus, record.uid, record.state = joined.address, bus, entry.uid, state


def bus_index(config: Config, opened: Sequence[str], name: str) -> int | None:
    """Which open bus a `joined.bus` names (zadania/zrobione/BUG-05.md, N8).

    By the name an open bus has (its channel — or `udp`, or a test's), or by the name
    the file gives a bus with that channel. When the hub has one bus, that one: a bus
    renamed in `hub.yaml`, or a trial on `--udp` with a real configuration, has nowhere
    else to be. With several and no match — `None`, not a guess.
    """
    if name in opened:
        return opened.index(name)
    for bus in config.buses:
        if bus.name == name and bus.channel in opened:
            return opened.index(bus.channel)
    return 0 if len(opened) == 1 else None


def bus_label(config: Config | None, opened: Sequence[str], index: int) -> str:
    """The name to write into `joined.bus` — the one from `hub.yaml` when the open bus
    is in it, otherwise the open bus's own."""
    name = opened[index] if 0 <= index < len(opened) else str(index)
    for bus in config.buses if config is not None else ():
        if bus.channel == name:
            return bus.name
    return name


def _issue(installation: Installation, text: str) -> None:
    """Once — `apply` runs again at every reload of the file."""
    if text not in installation.issues:
        installation.issues.append(text)


def _specs(binding: BindingConfig) -> list[BindingSpec]:
    source: Source = (
        Uid(binding.source.uid),
        binding.source.endpoint,
        binding.source.element,
    )
    return [_spec(source, binding.source, target) for target in binding.targets]


def _spec(source: tuple[object, int, int], origin: SourceRef, target: TargetRef) -> BindingSpec:
    return BindingSpec(
        source=source,  # type: ignore[arg-type]
        source_label=origin.label,
        source_name=origin.name,
        target_name=target.name,
        target_uid=target.uid,
        group=target.group,
        target_endpoint=target.endpoint,
        target_element=target.element,
        args=target.args,
        splices=target.splices,
        kind=target.kind,
        on_report=origin.on_report,
        label=target.label,
    )


__all__ = ["apply", "bus_index", "bus_label"]
