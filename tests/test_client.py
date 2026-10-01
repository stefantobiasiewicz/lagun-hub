"""The client of a running hub — the contract it shares with the hub itself."""

from __future__ import annotations

from lagunhub.adapters.client.client import Hub, HubClient
from lagunhub.api import HubApi
from tests.fake_hub import FakeHub


def test_hubapi_and_hubclient_satisfy_the_same_protocol() -> None:
    """If they stopped, a client would work with only one side — and that would come out
    only at a person's terminal."""

    def takes_it(_: Hub) -> None: ...

    takes_it(FakeHub())
    # A static check: mypy will reject this file if either of them drifts.
    _: type[Hub] = HubApi
    __: type[Hub] = HubClient
