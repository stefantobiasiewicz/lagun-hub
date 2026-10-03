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

from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    NodeConfig,
    SourceRef,
    TargetRef,
)
from lagunhub.core.model import Uid
from lagunhub.protocol.endpoints import by_name
from lagunhub.protocol.endpoints.definition import AttributeDef, encode_fields
from lagunhub.protocol.endpoints.enums import coerce
from lagunhub.protocol.registry import (
    FIRST_FUNCTIONAL_ENDPOINT,
    CommonAttribute,
    EndpointType,
    MessageKind,
)
from lagunhub.protocol.values import SCALARS, Value

FILES = ("hub.yaml", "nodes.yaml", "bindings.yaml")

CONFIG_ENV = "LAGUN_CONFIG"
"""The variable pointing at the configuration directory.

It exists because **a real installation’s configuration has no business living in a
clone of the repository**: `git pull` would collide with it at every update, and
`config/` in the repository is an example to read, not a starting point for a
house. Set once in `.bashrc`, it saves `--config` on every command."""


def default_config() -> Path:
    """The configuration directory: from the variable or `./config`. `--config` beats
    both."""
    import os

    return Path(os.environ.get(CONFIG_ENV) or "config")


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
    )


def _endpoint(
    alias: str, number: int, raw: dict[str, Any], groups: dict[int, GroupConfig]
) -> EndpointConfig:
    type_name = by_name(str(raw["type"])) if "type" in raw else None
    attributes: dict[int, Value] = {}
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


def _system_stays_enabled(meta: AttributeDef, value: Value, alias: str, number: int) -> None:
    """A system endpoint is always enabled — the node answers `Enabled = 0` with
    `INVALID_VALUE` (01-model-danych.md §4.1).

    Caught here rather than left to the node: accepted into the file, the value would
    have reconciling write it again and again, each time refused.
    """
    if number < FIRST_FUNCTIONAL_ENDPOINT and meta.id == CommonAttribute.Enabled and not value:
        raise ConfigError(
            f"{alias} ep{number}: a system endpoint cannot be disabled (01-model-danych.md §4.1)"
        )


def _value(meta: AttributeDef, raw: Any, alias: str, number: int) -> Value:
    value = _enum_or_number(meta, raw, alias, number)
    spec = SCALARS.get(meta.type)
    if spec is not None and isinstance(value, int) and not isinstance(value, bool):
        lo = meta.lo if meta.lo is not None else spec.lo
        hi = meta.hi if meta.hi is not None else spec.hi
        if not lo <= value <= hi:
            raise ConfigError(f"{alias} ep{number}: {meta.name} = {value} is outside {lo}…{hi}")
    return value


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
    raw_bytes = raw["to"]
    targets = tuple(
        _target(item, by_alias, groups)
        for item in (raw_bytes if isinstance(raw_bytes, list) else [raw_bytes])
    )
    return BindingConfig(source=source, targets=targets)


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
    given = raw.get("args") or {}
    try:
        return encode_fields(fields, given, grouped=grouped)
    except ValueError as exc:
        raise ConfigError(f"command arguments: {exc}") from None
