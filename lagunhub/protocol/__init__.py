"""The LagunBus specification in code.

This package maps onto the chapters of the specification, one to one:

| Module | Chapter |
|---|---|
| `frame.py` | `05` §1 — the 29-bit identifier |
| `bus/` | `05` — the mapping onto CAN, and transport |
| `values.py` | `02` — the data types |
| `quantities.py` | `02` §3 — quantities and scales |
| `registry.py` | `registry/` — **generated**, do not edit |
| `endpoints/` | `types/` — the catalogue of endpoint types |

**There is not a single hub decision in here.** That is why both the hub and the
simulated nodes use it — and the day it becomes worth carving out `lagunbus-core` is
the day this directory moves out on its own, carrying nothing else (point **H5**).
"""
