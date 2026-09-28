#!/usr/bin/env python3
"""Generates ``lagunhub/protocol/registry.py`` from the spec registry.

The registry in ``lagunbus-spec/registry/*.yaml`` is the declared source of truth for
identifiers. Copying them out by hand would drift from the specification at the first
change, and nobody would notice until something stopped working in a way that is hard
to explain.

Run with:  python tools/generate_registry.py [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

HUB = Path(__file__).resolve().parent.parent
REGISTRY_DIR = HUB.parent / "lagunbus-spec" / "registry"
TARGET = HUB / "lagunhub" / "protocol" / "registry.py"

# The registry still names frame kinds in Polish while the hub code is in English.
# The translation table lives HERE and is the only thing to update when the
# specification gains a message kind. An unknown name breaks generation — deliberately.
MESSAGE_KIND_NAMES = {
    "ALARM": "ALARM",
    "ZDARZENIE": "EVENT",
    "KOMENDA": "COMMAND",
    "KOMENDA_GRUPOWA": "GROUP_COMMAND",
    "ZAPIS": "WRITE",
    "ODCZYT": "READ",
    "ODPOWIEDŹ": "RESPONSE",
    "RAPORT": "REPORT",
    "ODPOWIEDŹ_DŁUGA": "LONG_RESPONSE",
    "ZAPIS_DŁUGI": "LONG_WRITE",
    "ZAPYTANIE": "QUERY",
    "ODPOWIEDŹ_HUBA": "HUB_RESPONSE",
    "PRZYŁĄCZANIE": "JOIN",
}


def load(name: str) -> dict[str, Any]:
    data = YAML(typ="safe").load(REGISTRY_DIR / name)
    if not isinstance(data, dict):
        raise SystemExit(f"{name}: expected a mapping at the top level")
    return data


def numbered(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries with a number assigned. Reserved ranges carry ``range``, not ``id``."""
    return sorted((e for e in entries if "id" in e), key=lambda e: int(e["id"]))


def parse_block(text: str) -> tuple[int, int]:
    """``"0x001-0x0FF"`` -> ``(1, 255)``. The registry uses an en dash, not a hyphen."""
    lo, _, hi = text.replace("–", "-").partition("-")
    return int(lo, 16), int(hi, 16)


def enum(name: str, doc: str, members: list[tuple[str, int, str]]) -> str:
    lines = [f"class {name}(IntEnum):", f'    """{doc}"""', ""]
    for member, value, comment in members:
        suffix = f"  # {comment}" if comment else ""
        lines.append(f"    {member} = {value:#04x}{suffix}")
    return "\n".join(lines) + "\n"


def build() -> str:
    kinds = load("frame-kinds.yaml")
    results = load("result-codes.yaml")
    classes = load("node-classes.yaml")
    types = load("endpoint-types.yaml")
    common = load("common-attributes.yaml")

    out: list[str] = [
        '"""LagunBus identifiers — A GENERATED FILE, do not edit.',
        "",
        "Source: lagunbus-spec/registry/*.yaml",
        "Generator: tools/generate_registry.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from enum import IntEnum",
        "",
        "",
    ]

    # --- message kinds ------------------------------------------------------
    kind_members: list[tuple[str, int, str]] = []
    transactional: list[str] = []
    for entry in numbered(kinds["kinds"]):
        polish = entry["name"]
        english = MESSAGE_KIND_NAMES.get(polish)
        if english is None:
            raise SystemExit(
                f"frame kind {polish!r} has no translation in MESSAGE_KIND_NAMES — "
                "add it in tools/generate_registry.py"
            )
        kind_members.append((english, int(entry["id"]), polish))
        if entry.get("transactional"):
            transactional.append(english)
    out.append(
        enum(
            "MessageKind",
            "The KIND field of an identifier. The number IS the priority (05-mapowanie-can.md §2).",
            kind_members,
        )
    )
    out.append("")
    out.append("TRANSACTIONAL: frozenset[MessageKind] = frozenset({")
    out.extend(f"    MessageKind.{n}," for n in transactional)
    out.append("})")
    out.append('"""Kinds that carry a Tid in payload byte [0]."""')
    out.append("")
    out.append("")

    # --- result codes -------------------------------------------------------
    result_members = [
        (e["name"], int(e["id"]), str(e.get("description", "")).strip().splitlines()[0])
        for e in numbered(results["codes"])
    ]
    out.append(enum("ResultCode", "Result codes of a reply (04-interakcje.md §9).", result_members))
    out.append("")
    retryable = [e["name"] for e in numbered(results["codes"]) if e.get("repeatable")]
    out.append("RETRYABLE: frozenset[ResultCode] = frozenset({")
    out.extend(f"    ResultCode.{n}," for n in retryable)
    out.append("})")
    out.append('"""The only codes after which a request is worth repeating."""')
    out.append("")
    out.append("")

    # --- node classes -------------------------------------------------------
    class_members = [(e["name"], int(e["id"]), "") for e in numbered(classes["classes"])]
    out.append(
        enum(
            "NodeClass",
            "The NodeClass field of an ANNOUNCEMENT — nothing but the choice of address "
            "block (05-mapowanie-can.md §4.1).",
            class_members,
        )
    )
    out.append("")
    out.append("NODE_CLASS_BLOCKS: dict[NodeClass, tuple[int, int]] = {")
    for e in numbered(classes["classes"]):
        lo, hi = parse_block(str(e["block"]))
        out.append(f"    NodeClass.{e['name']}: ({lo:#05x}, {hi:#05x}),")
    out.append("}")
    out.append('"""The address block for each class (03-adresacja.md §2.3)."""')
    out.append("")
    out.append("")

    # --- endpoint types -----------------------------------------------------
    type_members = [
        (e["name"], int(e["id"]), str(e.get("description", "")).strip().splitlines()[0])
        for e in numbered(types["types"])
    ]
    out.append(enum("EndpointType", "The catalogue of endpoint types.", type_members))
    out.append("")
    out.append("SYSTEM_ENDPOINTS: dict[int, EndpointType] = {")
    for e in sorted(
        (e for e in types["types"] if "fixed_endpoint" in e), key=lambda e: e["fixed_endpoint"]
    ):
        out.append(f"    {e['fixed_endpoint']}: EndpointType.{e['name']},")
    out.append("}")
    out.append('"""Endpoints whose number the specification fixes (01-model-danych.md §1.2)."""')
    out.append("")
    out.append("FIRST_FUNCTIONAL_ENDPOINT: int = 4")
    out.append(
        '"""Functional endpoints are numbered from 4 with no gaps '
        '(06-cykl-zycia-urzadzenia.md §3.2)."""'
    )
    out.append("")
    out.append("TYPE_REVISION: dict[EndpointType, int] = {")
    for e in numbered(types["types"]):
        if "revision" in e:
            out.append(f"    EndpointType.{e['name']}: {e['revision']},")
    out.append("}")
    out.append('"""The type contract revision this version of the hub knows."""')
    out.append("")
    out.append("")

    # --- common attributes --------------------------------------------------
    attr_members = [(e["name"], int(e["id"]), e["type"]) for e in numbered(common["attributes"])]
    out.append(
        enum(
            "CommonAttribute",
            "Attributes present on every functional endpoint (01-model-danych.md §4).",
            attr_members,
        )
    )
    out.append("")
    out.append("COMMON_ATTRIBUTE_TYPE: dict[CommonAttribute, str] = {")
    for e in numbered(common["attributes"]):
        out.append(f"    CommonAttribute.{e['name']}: {e['type']!r},")
    out.append("}")
    out.append("")
    out.append("COMMON_ATTRIBUTE_ACCESS: dict[CommonAttribute, str] = {")
    for e in numbered(common["attributes"]):
        out.append(f"    CommonAttribute.{e['name']}: {e['access']!r},")
    out.append("}")
    out.append("")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail when the file has drifted from the registry",
    )
    args = parser.parse_args()

    generated = build()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != generated:
            print(
                f"{TARGET.relative_to(HUB)} has drifted from the spec registry.\n"
                "Run: python tools/generate_registry.py",
                file=sys.stderr,
            )
            return 1
        print("the registry is in sync")
        return 0

    TARGET.write_text(generated, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(HUB)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
