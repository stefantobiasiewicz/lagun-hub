"""The behaviour of the endpoint types.

The definitions — numbers, types, ranges, command arguments — live in the hub's catalogue
``lagunhub.protocol.endpoints``. Here there is nothing but what a node does when
something arrives.
"""

from tests.node.endpoints.base import Endpoint, FunctionalEndpoint, SystemEndpoint
from tests.node.endpoints.device import Device
from tests.node.endpoints.diagnostics import Diagnostics
from tests.node.endpoints.display import Display
from tests.node.endpoints.groups import Groups
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.output import Output
from tests.node.endpoints.scenes import Scenes
from tests.node.endpoints.sense import Sense
from tests.node.endpoints.value_input import ValueInput

__all__ = [
    "Device",
    "Diagnostics",
    "Display",
    "Endpoint",
    "FunctionalEndpoint",
    "Groups",
    "KeyInput",
    "Output",
    "Scenes",
    "Sense",
    "SystemEndpoint",
    "ValueInput",
]
