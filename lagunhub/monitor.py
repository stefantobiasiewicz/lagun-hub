"""A human-readable watch on bus traffic.

A frame is self-describing — the kind, address, endpoint and element sit in the
identifier — so the watch needs neither the installation model nor anything beyond what
just arrived.
"""

from __future__ import annotations

from lagunhub.core.model import Installation
from lagunhub.protocol.frame import Frame, decode_header
from lagunhub.protocol.registry import MessageKind, ResultCode

LABEL = {
    MessageKind.ALARM: "ALARM",
    MessageKind.EVENT: "EVENT",
    MessageKind.COMMAND: "COMMAND",
    MessageKind.GROUP_COMMAND: "GROUP_CMD",
    MessageKind.WRITE: "WRITE",
    MessageKind.READ: "READ",
    MessageKind.RESPONSE: "RESPONSE",
    MessageKind.REPORT: "REPORT",
    MessageKind.LONG_RESPONSE: "LONG_RESP",
    MessageKind.LONG_WRITE: "LONG_WRITE",
    MessageKind.QUERY: "QUERY",
    MessageKind.HUB_RESPONSE: "HUB_RESP",
    MessageKind.JOIN: "JOIN",
}


def format_frame(frame: Frame, elapsed: float, bus: str = "") -> str:
    where = f"{bus:<6}" if bus else ""
    stamp = f"[{elapsed:7.3f}] {where}"
    if frame.is_join:
        join = frame.join
        direction = "ANNOUNCEMENT" if join.announce else "ASSIGNMENT"
        return f"{stamp}JOIN      {direction} short=…{join.short_id_low:06X}"

    fields = frame.fields
    try:
        label = LABEL[MessageKind(fields.kind)]
    except ValueError:
        label = f"0x{fields.kind:X}"
    detail = f"0x{fields.address:03X} ep{fields.endpoint} el=0x{fields.element:02X}"

    if fields.kind in (MessageKind.RESPONSE, MessageKind.HUB_RESPONSE) and frame.data:
        result, tid = decode_header(frame.data[0])
        try:
            result_name = ResultCode(result).name
        except ValueError:
            result_name = f"0x{result:02X}"
        detail += f" tid={tid} {result_name}"
        if len(frame.data) > 1:
            detail += f" [{frame.data[1:].hex(' ')}]"
    elif fields.kind in (MessageKind.EVENT, MessageKind.ALARM) and frame.data:
        detail += f" seq={frame.data[0]}"
        if len(frame.data) > 1:
            detail += f" [{frame.data[1:].hex(' ')}]"
    elif fields.kind in (MessageKind.COMMAND, MessageKind.READ, MessageKind.WRITE) and frame.data:
        _, tid = decode_header(frame.data[0])
        detail += f" tid={tid}"
        if len(frame.data) > 1:
            detail += f" [{frame.data[1:].hex(' ')}]"
    elif frame.data:
        detail += f" [{frame.data.hex(' ')}]"

    return f"{stamp}{label:<9} {detail}"


def format_installation(installation: Installation, now: float) -> str:
    """What the hub knows about the installation — **from listening alone**.

    It is worth looking at this with that in mind: after a restart the hub polls nobody
    ([`07-hub.md`](../lagunbus-spec/07-hub.md) §5.1), so everything below arrived by
    itself. A node that reports nothing will have only "heard" here.
    """
    unrecognised = [
        record
        for record in installation.records.values()
        if record.uid is None and record.short_id not in {0}
    ]
    if not installation.nodes and not unrecognised:
        return "The model is empty — the hub has not heard anybody yet."

    rows = [f"Installation model — {len(installation.nodes)} identified nodes"]
    for node in sorted(installation.nodes.values(), key=lambda n: n.address):
        heard_ago = f"{now - node.last_heard:.1f} s ago" if node.last_heard else "never"
        rows.append(
            f"\n0x{node.address:03X}  {node.name or node.uid.hex():<24} "
            f"bus {node.bus}  {node.state.name}  heard {heard_ago}"
        )
        if not node.endpoints:
            rows.append("       (reported nothing — the hub knows no more about it)")
        for endpoint in sorted(node.endpoints.values(), key=lambda e: e.number):
            type_name = endpoint.type_id.name if endpoint.type_id is not None else "unknown type"
            channel_text = f"ch{endpoint.channel}" if endpoint.channel else "ch0"
            features_text = f" features=0x{endpoint.features:04X}" if endpoint.features else ""
            rows.append(f"  ep{endpoint.number:<3} {type_name:<14} {channel_text}{features_text}")
            definition = endpoint.definition
            for number, state in sorted(endpoint.attributes.items()):
                meta = definition.attributes.get(number) if definition else None
                name = meta.name if meta else f"0x{number:02X}"
                value = state.value if state.value is not None else f"[{state.raw.hex(' ')}]"
                rows.append(f"         {name} = {value}")

    if unrecognised:
        rows.append(
            "\nJoining — these nodes have a ShortId, but the hub does not know their Uid yet"
        )
        for record in sorted(unrecognised, key=lambda r: r.short_id):
            address = f"0x{record.address:03X}" if record.address else "  —  "
            rows.append(
                f"  ShortId 0x{record.short_id:08X}  {address}  "
                f"{record.node_class.name:<11} {record.state.name}"
            )
        rows.append("  (the Uid is read by enumeration)")
    return "\n".join(rows)
