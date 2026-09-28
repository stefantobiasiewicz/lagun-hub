"""Common attributes — present on every endpoint, system ones included (01-model-danych.md §4).

The definition lives in [`definition.py`](definition.py), because `TypeDef` adds them
itself. This module is only the name one reaches for them by.
"""

from __future__ import annotations

from lagunhub.protocol.endpoints.definition import COMMON, COMMON_DEFAULTS

__all__ = ["COMMON", "COMMON_DEFAULTS"]
