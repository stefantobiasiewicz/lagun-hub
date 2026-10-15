"""Loading the configuration, and static validation.

Validation is **two-phase** ([`KONFIGURACJA.md`](../../KONFIGURACJA.md) §10). This phase
checks what can be checked **without access to hardware** — and it is stronger than it
looks, because the type catalogue knows that `Output` has a `Toggle` command and
`KeyInput` a `Click` event. A typo in `Toogle` is therefore caught when the file is
loaded.

The second phase, against a real installation, only comes once a node has been
identified — and it lands on the issue list rather than in an exception.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from lagunhub.core.config.model import (
    DEFAULT_API_PORT,
    DEFAULT_BITRATE,
    MAX_BITRATE,
    ApiConfig,
    BindingConfig,
    BusConfig,
    BusKind,
    Config,
    EndpointConfig,
    GroupConfig,
    JoinedConfig,
    NodeConfig,
    SourceRef,
    TargetRef,
)
from lagunhub.core.model import Uid, Wanted
from lagunhub.protocol.endpoints import by_name
from lagunhub.protocol.endpoints.definition import (
    QUANTITY_TYPE,
    AttributeDef,
    Field,
    encode_fields,
)
from lagunhub.protocol.endpoints.enums import coerce
from lagunhub.protocol.frame import ADDRESS_NODE_MAX, ADDRESS_NODE_MIN
from lagunhub.protocol.registry import (
    FIRST_FUNCTIONAL_ENDPOINT,
    CommonAttribute,
    EndpointType,
    MessageKind,
)
from lagunhub.protocol.values import SCALARS, Value, size_of

FILES = ("hub.yaml", "nodes.yaml", "bindings.yaml")

CONFIG_ENV = "LAGUN_CONFIG"
"""The variable pointing at the configuration directory.

It exists because **a real installation’s configuration has no business living in a
clone of the repository**: `git pull` would collide with it at every update, and
`config/` in the repository is an example to read, not a starting point for a
house. Set once in `.bashrc`, it saves `--config` on every command."""


DEFAULT_CONFIG: Final = "~/lagun"
"""Where the configuration lives when nothing says otherwise — the place `init` and the
README have always advised.

It used to be `./config`, relative to wherever the hub was started. Now that `run` sets up
a clean configuration where it finds none, that default would plant a second, empty
installation in any directory the hub happened to be started from — a service with
`cwd=/` included. One fixed place means one installation, and `tui` finds it the same
way. The example in the repository is still `--config config/`, said out loud."""


def default_config() -> Path:
    """The configuration directory: from the variable or `~/lagun`. `--config` beats
    both."""
    import os

    return Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser()


class ConfigError(ValueError):
    """The configuration is invalid. The hub will not start with it — and is not to
    pretend it did."""


def load(where: Path) -> Config:
    """Load the `config/` directory or a single file.

    Every disagreement with the type catalogue — an unknown type, a typo in an element
    name — comes out of here as a `ConfigError` rather than an exception from deep
    inside the codec.
    """
    try:
        return _load(where)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def from_document(document: Mapping[str, Any]) -> Config:
    """Validating content that is **not in any file yet**.

    A write through the API has to check the new configuration before it writes
    anything — otherwise "invalid configuration" would mean "a broken file"
    ([`KONFIGURACJA.md`](../../../KONFIGURACJA.md) §11.3, step 1).
    """
    try:
        return _document(document)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _load(where: Path) -> Config:
    return _document(_read(where))


def _document(document: Mapping[str, Any]) -> Config:
    groups = _groups(document.get("groups") or {})
    buses = _buses(document.get("buses") or ["vcan0"])

    nodes: list[NodeConfig] = []
    by_alias: dict[str, NodeConfig] = {}
    for key, raw in (document.get("nodes") or {}).items():
        entry = _node(str(key), raw or {}, groups)
        if entry.alias in by_alias:
            raise ConfigError(f"alias {entry.alias!r} used twice")
        nodes.append(entry)
        by_alias[entry.alias] = entry
        # A node may be referred to by alias or by Uid directly — the latter works even
        # when nobody has given it an alias yet.
        by_alias.setdefault(entry.uid.hex(), entry)

    bindings = tuple(_binding(raw, by_alias, groups) for raw in (document.get("bindings") or []))
    return Config(
        buses=buses,
        nodes=tuple(nodes),
        bindings=bindings,
        groups=groups,
        accept_new=bool(document.get("accept_new", False)),
        api=_api(document.get("api") or {}),
    )


def _api(raw: Any) -> ApiConfig:
    if not isinstance(raw, dict):
        raise ConfigError("`api` is to be a mapping: enabled, host, port, token")
    strangers = set(raw) - {"enabled", "host", "port", "token"}
    if strangers:
        raise ConfigError(f"`api`: unknown keys {', '.join(sorted(strangers))}")
    port = int(raw.get("port", DEFAULT_API_PORT))
    if not 1 <= port <= 65535:
        raise ConfigError(f"`api.port` = {port} is outside 1…65535")
    return ApiConfig(
        enabled=bool(raw.get("enabled", True)),
        host=str(raw.get("host", "127.0.0.1")),
        port=port,
        token=str(raw.get("token", "")),
    )


def _read(where: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    if where.is_dir():
        merged: dict[str, Any] = {}
        for name in FILES:
            file_ = where / name
            if not file_.exists():
                continue
            part = yaml.load(file_)
            if part:
                merged.update(part)
        if not merged:
            raise ConfigError(f"{where}: none of the files {', '.join(FILES)} is present")
        return merged
    if not where.exists():
        raise ConfigError(f"{where}: no such file")
    document = yaml.load(where)
    if not isinstance(document, dict):
        raise ConfigError(f"{where}: expected a mapping at the top level")
    return document


def _groups(raw: dict[Any, Any]) -> dict[int, GroupConfig]:
    out: dict[int, GroupConfig] = {}
    for key, description in raw.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            raise ConfigError(
                f"a group's key is its NUMBER, not its name: {key!r} "
                "(the name goes in the `name` field)"
            ) from None
        if not 0x001 <= number <= 0x7FF:
            raise ConfigError(f"group number {number} is outside 0x001–0x7FF")
        name = (
            str((description or {}).get("name", ""))
            if isinstance(description, dict)
            else str(description or "")
        )
        out[number] = GroupConfig(number=number, name=name)
    return out


def _buses(raw: list[Any]) -> tuple[BusConfig, ...]:
    buses = tuple(_bus(entry) for entry in raw)
    seen: set[str] = set()
    for bus in buses:
        # Two sockets bound to one interface would each hear every frame — every event
        # carried out twice, every response matched twice.
        if bus.channel in seen:
            raise ConfigError(f"channel {bus.channel} is used twice in `buses`")
        seen.add(bus.channel)
    return buses


def _bus(raw: str | dict[str, Any]) -> BusConfig:
    """A bus entry. A bare name and an entry without `kind` are SocketCAN — the shape
    files had before FEAT-01, and they keep loading."""
    if isinstance(raw, str):
        return BusConfig(name=raw, channel=raw)
    label = str(raw.get("name") or raw.get("channel") or "?")
    kinds = ", ".join(kind.value for kind in BusKind)
    try:
        kind = BusKind(str(raw.get("kind", BusKind.SOCKETCAN.value)))
    except ValueError:
        raise ConfigError(
            f"bus {label!r}: unknown kind {str(raw['kind'])!r} — known: {kinds}"
        ) from None
    if not raw.get("channel"):
        raise ConfigError(f"bus {label!r}: a {kind.value} bus needs a `channel` (e.g. can0)")
    channel = str(raw["channel"])
    bitrate = raw.get("bitrate", DEFAULT_BITRATE)
    if isinstance(bitrate, bool) or not isinstance(bitrate, int):
        raise ConfigError(f"bus {label!r}: bitrate {bitrate!r} is not a number of bit/s")
    if not 0 < bitrate <= MAX_BITRATE:
        raise ConfigError(f"bus {label!r}: bitrate {bitrate} is outside 1…{MAX_BITRATE} bit/s")
    return BusConfig(
        name=str(raw.get("name", channel)), channel=channel, bitrate=bitrate, kind=kind
    )


def _uid(text: str) -> Uid:
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ConfigError(f"a Uid is not hexadecimal: {text!r}") from None
    if not 8 <= len(raw) <= 16:
        raise ConfigError(f"a Uid is 8-16 bytes, got {len(raw)}: {text!r}")
    return Uid(raw)


def _node(key: str, raw: dict[str, Any], groups: dict[int, GroupConfig]) -> NodeConfig:
    """The key is the **`Uid`**, because that is the identity.

    An alias is a label a person gives — readability of the file, not identity. Keeping
    it in the key would suggest the hub knows that "kitchen-relay is at this address";
    all it knows is that somebody named **this particular `Uid`** that way.

    Two problems disappear along the way: YAML itself enforces `Uid` uniqueness, and an
    API adding a node need not invent an alias that does not exist yet (K9).
    """
    uid = _uid(str(raw["uid"]) if "uid" in raw else key)
    alias = str(raw.get("alias", "")) or key
    endpoints: dict[int, EndpointConfig] = {}
    for number, description in (raw.get("endpoints") or {}).items():
        endpoints[int(number)] = _endpoint(alias, int(number), description or {}, groups)
    blocked = raw.get("blocked", False)
    if not isinstance(blocked, bool):
        raise ConfigError(f"{alias}: `blocked` is true or false, got {blocked!r}")
    return NodeConfig(
        alias=alias,
        uid=uid,
        name=str(raw.get("name", "")),
        room=str(raw.get("room", "")),
        endpoints=endpoints,
        blocked=blocked,
        joined=_joined(alias, raw["joined"]) if raw.get("joined") is not None else None,
    )


def _joined(alias: str, raw: Any) -> JoinedConfig:
    """What the hub wrote. Checked as strictly as anything a person wrote — a hand edit
    that breaks it must stop the file, not put a node at an address it does not hold."""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{alias}: `joined` is a mapping of address, bus and short_id")
    missing = {"address", "bus", "short_id"} - set(raw)
    if missing:
        raise ConfigError(f"{alias}: `joined` lacks {', '.join(sorted(missing))}")
    address = _number(alias, "address", raw["address"])
    if not ADDRESS_NODE_MIN <= address <= ADDRESS_NODE_MAX:
        raise ConfigError(
            f"{alias}: `joined.address` {address:#05x} is not a node address "
            f"(0x{ADDRESS_NODE_MIN:03X}-0x{ADDRESS_NODE_MAX:03X})"
        )
    short_id = _number(alias, "short_id", raw["short_id"])
    if not 0 <= short_id <= 0xFFFF_FFFF:
        raise ConfigError(f"{alias}: `joined.short_id` is 32 bits, got {short_id:#x}")
    bus = str(raw["bus"])
    if not bus:
        raise ConfigError(f"{alias}: `joined.bus` is empty")
    return JoinedConfig(address=address, bus=bus, short_id=short_id)


def _number(alias: str, key: str, raw: Any) -> int:
    """An integer written as `0x012` or `18` — YAML reads the first as a number already,
    a quoted one comes as text."""
    if isinstance(raw, bool):
        raise ConfigError(f"{alias}: `joined.{key}` is a number, got {raw!r}")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 0)
    except ValueError:
        raise ConfigError(f"{alias}: `joined.{key}` is a number, got {raw!r}") from None


def _endpoint(
    alias: str, number: int, raw: dict[str, Any], groups: dict[int, GroupConfig]
) -> EndpointConfig:
    type_name = by_name(str(raw["type"])) if "type" in raw else None
    attributes: dict[int, Wanted] = {}
    for name, value in raw.items():
        if name in ("type", "name", "groups"):
            continue
        if type_name is None:
            raise ConfigError(
                f"{alias} ep{number}: to set {name!r}, the endpoint has to give a `type`"
            )
        meta = _attribute(type_name.name, type_name.attribute(str(name)), alias, number)
        attributes[meta.id] = _value(meta, value, alias, number)
        _system_stays_enabled(meta, attributes[meta.id], alias, number)
    return EndpointConfig(
        type=EndpointType[type_name.name] if type_name is not None else None,
        name=str(raw.get("name", "")),
        attributes=attributes,
        groups=tuple(_group(g, groups) for g in (raw.get("groups") or [])),
    )


def _attribute(type_name: str, meta: AttributeDef, alias: str, number: int) -> AttributeDef:
    """Configuration means `RW` and persistent attributes — the rest is state or a working
    setting."""
    if not (meta.writable and meta.persist):
        kind = "state" if not meta.writable else "a working setting"
        raise ConfigError(
            f"{alias} ep{number}: {type_name}.{meta.name} is {kind}, not configuration "
            "(KONFIGURACJA.md §7)"
        )
    return meta


def _system_stays_enabled(meta: AttributeDef, value: Wanted, alias: str, number: int) -> None:
    """A system endpoint is always enabled — the node answers `Enabled = 0` with
    `INVALID_VALUE` (01-model-danych.md §4.1).

    Caught here rather than left to the node: accepted into the file, the value would
    have reconciling write it again and again, each time refused.
    """
    if number < FIRST_FUNCTIONAL_ENDPOINT and meta.id == CommonAttribute.Enabled and not value:
        raise ConfigError(
            f"{alias} ep{number}: a system endpoint cannot be disabled (01-model-danych.md §4.1)"
        )


def _value(meta: AttributeDef, raw: Any, alias: str, number: int) -> Wanted:
    if meta.in_units:
        return _in_units(meta, raw, alias, number)
    value = _enum_or_number(meta, raw, alias, number)
    spec = SCALARS.get(meta.type)
    if spec is not None and isinstance(value, int) and not isinstance(value, bool):
        lo = meta.lo if meta.lo is not None else spec.lo
        hi = meta.hi if meta.hi is not None else spec.hi
        if not lo <= value <= hi:
            raise ConfigError(f"{alias} ep{number}: {meta.name} = {value} is outside {lo}…{hi}")
    return value


def _in_units(meta: AttributeDef, raw: Any, alias: str, number: int) -> float:
    """`ThresholdHigh: 26.5` — a number in the units of the endpoint's quantity, which
    the file does not know: what `26.5` is on the wire, and whether it fits, is settled
    after enumeration (`EndpointModel.on_wire`; KONFIGURACJA.md §7.2, §10). Here only
    that it is a number — a word is a typo the hub refuses to start with."""
    if not isinstance(raw, bool):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    raise ConfigError(
        f"{alias} ep{number}: {meta.name} = {raw!r} is not a number in the units of the "
        "endpoint's quantity"
    )


def _enum_or_number(meta: AttributeDef, raw: Any, alias: str, number: int) -> Value:
    """The name table is shared with the API — the file and the front end are to speak the
    same vocabulary."""
    try:
        return coerce(meta, raw)
    except ValueError as exc:
        raise ConfigError(f"{alias} ep{number}: {exc}") from None


def _group(raw: Any, groups: dict[int, GroupConfig]) -> int:
    """A group may be referred to by number or by name — just like a node."""
    if isinstance(raw, int):
        if raw not in groups:
            raise ConfigError(f"group {raw} is not defined in `groups`")
        return raw
    for number, group in groups.items():
        if group.name == str(raw):
            return number
    familiar = ", ".join(sorted(g.name for g in groups.values() if g.name)) or "none"
    raise ConfigError(f"unknown group {raw!r}; defined: {familiar}")


def _binding(
    raw: dict[str, Any], by_alias: dict[str, NodeConfig], groups: dict[int, GroupConfig]
) -> BindingConfig:
    source = _source(raw["from"], by_alias)
    raw_targets = raw["to"]
    items = raw_targets if isinstance(raw_targets, list) else [raw_targets]
    targets = tuple(_target(item, by_alias, groups) for item in items)
    for item, target in zip(items, targets, strict=True):
        if target.kind == MessageKind.WRITE:
            _carries_value(raw["from"], item, source)
    targets = tuple(
        _with_splices(raw["from"], item, source, target)
        for item, target in zip(items, targets, strict=True)
    )
    return BindingConfig(source=source, targets=targets)


FROM_EVENT: Final = "$"
"""An argument written `$value` is the source event's field `value` (07-hub.md §3.2)."""


def _references(raw_target: dict[str, Any]) -> dict[str, str]:
    """`{argument: event field}` for every argument the file writes as `$field`."""
    given = raw_target.get("args") or {}
    return {
        name: value[len(FROM_EVENT) :]
        for name, value in given.items()
        if isinstance(value, str) and value.startswith(FROM_EVENT)
    }


def _offsets(fields: tuple[Field, ...]) -> dict[str, tuple[int, str]]:
    """Each field's offset in its payload and its type. A `<Q>` field has no size
    until the node's `Quantity` is known, so it cannot be copied by offset — refused."""
    out: dict[str, tuple[int, str]] = {}
    offset = 0
    for f in fields:
        if f.type == QUANTITY_TYPE:
            out[f.name] = (-1, f.type)
            continue  # nothing after it has a fixed offset either
        out[f.name] = (offset, f.type)
        offset += size_of(f.type)
    return out


def _with_splices(
    raw_source: dict[str, Any], raw_target: dict[str, Any], source: SourceRef, target: TargetRef
) -> TargetRef:
    """Arguments from the source event, copied byte for byte (07-hub.md §3.2;
    zadania/devices/DEVICE-02.md N1).

    Checked here so the fast path only copies: the source is an event, the field exists,
    and it is of the argument's type — the hub converts nothing, as with a write
    (FEAT-13 N2). Offsets count the event's sequence number and the command's header,
    byte 0 of each payload. A group command is left out: a value into many targets is
    the bindings epic's."""
    references = _references(raw_target)
    if not references:
        return target
    label = source.label
    if source.on_report:
        raise ConfigError(
            f"{label}: an argument from the source (`$…`) needs an event as the source"
        )
    if target.kind != MessageKind.COMMAND:
        raise ConfigError(f"{label}: an argument from the source goes only to a node's command")
    event = by_name(str(raw_source["type"])).event(str(raw_source["event"]))
    command = by_name(str(raw_target["type"])).command(str(raw_target["command"]))
    given = _offsets(event.params)
    wanted = _offsets(command.args)
    splices = []
    for argument, field in references.items():
        if field not in given:
            known = ", ".join(given) or "none"
            raise ConfigError(f"{label}: {event.name} has no field {field!r}; it has: {known}")
        src, src_type = given[field]
        dst, dst_type = wanted[argument]
        if src < 0 or dst < 0:
            raise ConfigError(
                f"{label}: ${field} → {argument}: a value typed by Quantity cannot be copied yet"
            )
        if src_type != dst_type:
            raise ConfigError(
                f"{label}: ${field} is a {src_type} and {argument} a {dst_type} — the hub "
                "copies the value and converts nothing"
            )
        splices.append((1 + src, 1 + dst, size_of(src_type)))
    return dataclasses.replace(target, splices=tuple(splices))


def _carries_value(
    raw_source: dict[str, Any], raw_target: dict[str, Any], source: SourceRef
) -> None:
    """A write carries the source's value, copied byte for byte (07-hub.md §3.2).

    So the source has to be an attribute — an event carries no attribute value, and
    before this check such a binding sent an empty write the node refused — and of the
    same type, because the hub converts nothing (zadania/zrobione/FEAT-13.md N2, N9). `<Q>`
    against `<Q>` passes: whether the two quantities agree is known only from the nodes,
    and the hub does not check it yet (N4). A string never gets here — no reported
    attribute is one, and a binding write goes as a plain WRITE.
    """
    if not source.on_report:
        raise ConfigError(
            f"{source.label}: a write needs an attribute as its source, not an event — "
            "an event carries no value to write"
        )
    given = by_name(str(raw_source["type"])).attribute(str(raw_source["attribute"])).type
    wanted = by_name(str(raw_target["type"])).attribute(str(raw_target["attribute"])).type
    if given != wanted:
        raise ConfigError(
            f"{source.label}: a {given} cannot be written into a {wanted} — the hub copies "
            "the value and converts nothing"
        )


def _alias(raw: dict[str, Any], by_alias: dict[str, NodeConfig]) -> NodeConfig:
    alias = str(raw["node"])
    entry = by_alias.get(alias)
    if entry is None:
        familiar = ", ".join(sorted(by_alias)) or "none"
        raise ConfigError(
            f"the binding points at node {alias!r}, which does not exist; known: {familiar}"
        )
    return entry


def _source(raw: dict[str, Any], by_alias: dict[str, NodeConfig]) -> SourceRef:
    entry = _alias(raw, by_alias)
    endpoint = int(raw["endpoint"])
    type_name = by_name(str(raw["type"]))
    if "event" in raw:
        element = type_name.event(str(raw["event"]))
        return SourceRef(
            uid=entry.uid,
            endpoint=endpoint,
            element=element.id,
            label=f"{entry.alias} ep{endpoint} {element.name}",
        )
    if "attribute" not in raw:
        raise ConfigError(
            f"{entry.alias} ep{endpoint}: the source is either `event` or `attribute`"
        )
    meta = type_name.attribute(str(raw["attribute"]))
    if not meta.reported:
        # Only a REPORTED attribute may be a source — otherwise the hub would never
        # learn about the change (07-hub.md §3.1).
        raise ConfigError(
            f"{entry.alias} ep{endpoint}: {type_name.name}.{meta.name} is not reported"
        )
    return SourceRef(
        uid=entry.uid,
        endpoint=endpoint,
        element=meta.id,
        on_report=True,
        label=f"{entry.alias} ep{endpoint} {meta.name}",
    )


def _target(
    raw: dict[str, Any], by_alias: dict[str, NodeConfig], groups: dict[int, GroupConfig]
) -> TargetRef:
    type_name = by_name(str(raw["type"]))
    endpoint = int(raw.get("endpoint", 0))

    if "group" in raw:
        number = _group(raw["group"], groups)
        command = type_name.command(str(raw["command"]))
        return TargetRef(
            kind=MessageKind.GROUP_COMMAND,
            group=number,
            endpoint=endpoint,
            element=command.id,
            args=_args(command.args, raw, grouped=True),
            label=f"group {number} ep{endpoint} {type_name.name}.{command.name}",
        )

    entry = _alias(raw, by_alias)
    if "attribute" in raw:
        meta = type_name.attribute(str(raw["attribute"]))
        if not meta.writable:
            raise ConfigError(f"{entry.alias} ep{endpoint}: {type_name.name}.{meta.name} is RO")
        return TargetRef(
            kind=MessageKind.WRITE,
            uid=entry.uid,
            endpoint=endpoint,
            element=meta.id,
            label=f"{entry.alias} ep{endpoint} {type_name.name}.{meta.name} ←",
        )

    command = type_name.command(str(raw["command"]))
    return TargetRef(
        kind=MessageKind.COMMAND,
        uid=entry.uid,
        endpoint=endpoint,
        element=command.id,
        args=_args(command.args, raw, grouped=False),
        label=f"{entry.alias} ep{endpoint} {type_name.name}.{command.name}",
    )


def _args(fields: tuple[Any, ...], raw: dict[str, Any], *, grouped: bool) -> bytes:
    given = dict(raw.get("args") or {})
    references = _references(raw)
    if references and grouped:
        raise ConfigError("an argument from the source (`$…`) cannot go to a group command yet")
    for name in references:
        if name not in {f.name for f in fields}:
            raise ConfigError(f"command arguments: unknown argument {name!r}")
        given[name] = 0  # a place for the event's bytes, filled on the fast path
    try:
        return encode_fields(fields, given, grouped=grouped)
    except ValueError as exc:
        raise ConfigError(f"command arguments: {exc}") from None
