"""Adapters — the only place where the hub talks to the world.

Each of them stands on [`api/`](../api/) and **nothing more**: it does not know the
model, does not know the bus and has no list of operations of its own. That is why
adding a fourth changes not a single line in the core
([`ARCHITEKTURA.md`](../../ARCHITEKTURA.md) §10).
"""
