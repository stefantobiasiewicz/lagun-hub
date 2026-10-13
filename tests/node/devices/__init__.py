"""Ready-made devices — a descriptor plus chosen defaults.

The ``DeviceType`` numbers are a DEBT, not a decision: the specification's registry
divides the space by physical form, and a node double has no physical form
(ARCHITEKTURA.md §7.1, item S1).
"""

from tests.node.devices.display2 import display2
from tests.node.devices.key1 import key1
from tests.node.devices.panel3 import panel3
from tests.node.devices.relay1 import relay1

__all__ = ["display2", "key1", "panel3", "relay1"]
