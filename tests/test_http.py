"""The hub server — REST and SSE.

The only tests in this directory that run **on real threads and a real clock**: they
check the transport, not the hub's logic, so faking time would give nothing here. The rest
stay with a clock driven from the test.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from lagunhub.adapters.client.client import _build
from lagunhub.adapters.http.server import HubServer, free_port
from lagunhub.api import BusSetupView, HealthView, HubApi
from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config import load
from lagunhub.core.config.apply import apply
from lagunhub.core.config.store import ConfigStore
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import Installation, Snapshot
from lagunhub.core.rx import RxThread, Sent
from lagunhub.drivers.base import Rejected
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.endpoints import output as out
from tests.node.devices import relay1
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore
from tests.test_buses import BITRATE, FakeDriver

RELAY_UID = bytes(range(8, 16))
TOKEN = "test-token"

CONFIG = """
buses: [vcan0]
nodes:
  "08090a0b0c0d0e0f":
    alias: kitchen-relay
    name: "The lamp over the table"
    endpoints:
      4: { type: Output, Purpose: LIGHT }
"""


class Hub:
    """A hub on real threads, with a node and a server. Everything on one port."""

    def __init__(self, tmp_path: Any) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG, encoding="utf-8")

        self.net = VirtualNetwork()
        port = self.net.attach("hub")
        self.installation = Installation()
        apply(self.installation, load(path))
        self.config_path = path

        self.engine = BindingEngine([port], self.installation.snapshot())
        self.rx = RxThread([port], self.engine)
        self.engine.on_sent = lambda bus, frame: self.rx.offer(Sent(bus, frame, time.monotonic()))

        def publish(snapshot: Snapshot) -> None:
            self.engine.snapshot = snapshot

        self.store = ConfigStore(path)
        self.core = CoreThread(
            self.installation, self.rx.cold, publish, buses=[port], config_store=self.store
        )
        self.core.meter.watch(received=[self.rx.counts], sent=[self.engine.counts])
        self.core.config = load(path)  # as `lagun-hub run` does
        self.driver = FakeDriver([BITRATE])
        self.core.commissioning.accept_new(300.0)
        self.relay: NodeCore = relay1(uid=RELAY_UID, address=0, name="relay")
        self.relay.start(self.net.attach("relay"))
        # A real node draws a delay of 0…2 s before its first ANNOUNCEMENT so that forty
        # at once do not collide on the bus. Here there is nothing to collide with, and the
        # delay would cost a second in every test case.
        assert self.relay.joining is not None
        self.relay.joining.next_attempt = 0.0

        self._stop = threading.Event()
        self._pump = threading.Thread(target=self._loop, daemon=True)
        self.server = HubServer(
            HubApi(self.core, engine=self.engine, drivers=lambda bus: self.driver),
            host="127.0.0.1",
            port=free_port(),
            token=TOKEN,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.relay.poll()
            self.relay.tick()
            time.sleep(0.002)

    def start(self) -> None:
        self.rx.start()
        self.core.start()
        self._pump.start()
        self.server.start()
        self.until(lambda: bool(self.installation.nodes), "the node did not enter the installation")
        # Reconciling announces state changes too — and rightly, because a front end is to
        # see the hub bringing a node into agreement. The test starts only after that, so as
        # not to take
        # somebody else's event for its own.
        self.until(lambda: len(self.core.reconcile) == 0, "reconciling did not go quiet")

    def stop(self) -> None:
        self.server.stop()
        self._stop.set()
        self._pump.join(timeout=1.0)
        self.rx.stop()
        self.core.stop()

    @staticmethod
    def until(condition: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.005)
        raise AssertionError(f"waited in vain for: {what}")

    # --- the client -------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = TOKEN,
        etag: str | None = None,
    ) -> tuple[int, Any]:
        status, content, _ = self.full(method, path, body, token=token, etag=etag)
        return status, content

    def full(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = TOKEN,
        etag: str | None = None,
    ) -> tuple[int, Any, str]:
        """Like `call`, but gives the `ETag` back too — without it nothing can be
        written."""
        connection = http.client.HTTPConnection(*self.server.address, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        if etag is not None:
            headers["If-Match"] = f'"{etag}"'
        content = json.dumps(body).encode() if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=content, headers=headers)
            response = connection.getresponse()
            raw_bytes = response.read()
            return (
                response.status,
                (json.loads(raw_bytes) if raw_bytes else None),
                (response.getheader("ETag") or "").strip('"'),
            )
        finally:
            connection.close()

    def etag(self) -> str:
        return self.full("GET", "/config")[2]

    def get(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return self.call("GET", path, **kwargs)


@pytest.fixture
def hub(tmp_path: Any) -> Iterator[Hub]:
    instance = Hub(tmp_path)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


class TestAuthentication:
    def test_without_a_token_nothing_comes_out(self, hub: Hub) -> None:
        """The hub drives a gate and a lock. "No token by default" means in practice "no
        token at anybody's" — and that is why there is no such mode (H6)."""
        status, _ = hub.get("/nodes", token=None)
        assert status == 401

    def test_a_bad_token_does_not_get_through(self, hub: Hub) -> None:
        assert hub.get("/nodes", token="almost-right")[0] == 401

    def test_a_token_in_a_query_parameter_works_for_sse(self, hub: Hub) -> None:
        """The browser's `EventSource` cannot set a header — and that is the only
        reason."""
        status, _ = hub.get(f"/nodes?token={TOKEN}", token=None)
        assert status == 200

    def test_the_token_does_not_leak_in_a_response(self, hub: Hub) -> None:
        _, info = hub.get("/")
        assert TOKEN not in json.dumps(info)


class TestReading:
    def test_the_root_says_what_one_is_talking_to(self, hub: Hub) -> None:
        status, info = hub.get("/")
        assert status == 200
        assert info["name"] == "lagun-hub"
        assert info["buses"] == ["hub"]

    def test_the_node_list(self, hub: Hub) -> None:
        status, items = hub.get("/nodes")
        assert status == 200
        assert [n["alias"] for n in items] == ["kitchen-relay"]
        assert items[0]["uid"] == RELAY_UID.hex()

    def test_a_node_by_alias_and_by_uid(self, hub: Hub) -> None:
        _, by_alias = hub.get("/nodes/kitchen-relay")
        _, po_uid = hub.get(f"/nodes/{RELAY_UID.hex()}")
        assert by_alias["uid"] == po_uid["uid"]
        assert any(e["number"] == 4 for e in by_alias["detail"])

    def test_an_endpoint_carries_configuration_next_to_actual(self, hub: Hub) -> None:
        _, ep4 = hub.get("/nodes/kitchen-relay/ep/4")
        purpose = next(a for a in ep4["attributes"] if a["name"] == "Purpose")
        assert purpose["wanted_label"] == "LIGHT"
        assert purpose["matches"] is True

    def test_health_and_bindings_answer(self, hub: Hub) -> None:
        assert hub.get("/health")[1]["ready"] == 1
        assert hub.get("/bindings")[1] == []
        assert hub.get("/issues")[1] == []
        assert hub.get("/commissioning")[1]["accepting"] is True

    def test_health_carries_traffic_per_bus(self, hub: Hub) -> None:
        """The relay joined, was read and reconciled — traffic both ways, and the client
        turns it back into the same view (zadania/zrobione/FEAT-05.md)."""
        _, body = hub.get("/health")
        (bus,) = body["buses"]
        assert bus["name"] == "hub"
        assert bus["rx"] > 0 and bus["tx"] > 0
        assert bus["bitrate"] is None and bus["load"] is None, "`hub` is not a channel in the file"
        assert body["uptime"] > 0

        health = _build(HealthView, body)
        assert health.buses[0].rx == bus["rx"]
        assert health.buses[0].load is None

    def test_admit_and_reset_are_routed_and_refuse_what_is_not_pending(self, hub: Hub) -> None:
        """The joined relay is READY, not a stranger — both operations refuse it outright
        instead of touching a working node."""
        short_id = hub.get("/commissioning")[1]["known"][0]["short_id"]
        for action in ("admit", "factory-reset"):
            status, body = hub.call("POST", f"/commissioning/{short_id:08X}/{action}")
            assert status == 400, body
        assert hub.call("POST", "/commissioning/nothex/admit")[0] == 404


class TestConfigurationEndpoint:
    """A write goes to the FILE and reaches the node by reconciling — never the other way
    round."""

    ENDPOINT = "/config/nodes/08090a0b0c0d0e0f/endpoints/4"

    def test_a_read_yields_the_document_and_its_version(self, hub: Hub) -> None:
        status, view, etag = hub.full("GET", "/config")
        assert status == 200
        assert etag.startswith("sha256:") and etag == view["etag"]
        assert view["document"]["nodes"]["08090a0b0c0d0e0f"]["alias"] == "kitchen-relay"

    def test_a_write_without_if_match_is_blind_and_rejected(self, hub: Hub) -> None:
        """There is no "overwrite anyway" mode: whoever wants to overwrite first sees
        what."""
        status, error = hub.call("PUT", f"{self.ENDPOINT}/AutoOffTime", {"value": 30})
        assert status == 400
        assert "If-Match" in error["message"]

    def test_a_stale_etag_is_409(self, hub: Hub) -> None:
        status, error = hub.call(
            "PUT", f"{self.ENDPOINT}/AutoOffTime", {"value": 30}, etag="sha256:stare"
        )
        assert status == 409
        assert error["problem"] == "conflict"

    def test_invalid_configuration_is_422_and_leaves_no_trace(self, hub: Hub) -> None:
        before = hub.config_path.read_text(encoding="utf-8")
        status, error = hub.call("PUT", f"{self.ENDPOINT}/Zmywarka", {"value": 1}, etag=hub.etag())
        assert status == 422
        assert error["problem"] == "rejected"
        assert hub.config_path.read_text(encoding="utf-8") == before

    def test_a_write_lands_in_the_file_and_reaches_the_node(self, hub: Hub) -> None:
        """The whole road from KONFIGURACJA.md §11.1: API → file → reconciling → node."""
        status, view, fresh = hub.full(
            "PUT", f"{self.ENDPOINT}/AutoOffTime", {"value": 30}, etag=hub.etag()
        )
        assert status == 200
        assert fresh != "" and fresh == view["etag"]
        assert "AutoOffTime" in hub.config_path.read_text(encoding="utf-8")
        hub.until(
            lambda: hub.relay.endpoint(4, Output).values[out.AUTO_OFF_TIME] == 30,
            "the value did not reach the node",
        )

    def test_deletion_works(self, hub: Hub) -> None:
        status, _ = hub.call("DELETE", f"{self.ENDPOINT}/Purpose", etag=hub.etag())
        assert status == 200
        assert "Purpose" not in hub.config_path.read_text(encoding="utf-8")

    def test_the_drift_is_visible_outright(self, hub: Hub) -> None:
        status, drift = hub.get("/diff")
        assert status == 200
        purpose = next(r for r in drift if r["attribute"] == "Purpose")
        assert purpose["state"] == "match"
        assert purpose["wanted_label"] == "LIGHT"


class TestErrors:
    def test_an_unknown_node_is_404(self, hub: Hub) -> None:
        status, error = hub.get("/nodes/cellar")
        assert status == 404
        assert error["problem"] == "not_found"

    def test_an_unknown_path_is_404(self, hub: Hub) -> None:
        assert hub.get("/no-such-thing")[0] == 404

    def test_a_wrong_method_is_405_not_404(self, hub: Hub) -> None:
        """A typo in the method is not to look like a typo in the path."""
        status, error = hub.call("PUT", "/nodes")
        assert status == 405
        assert error["problem"] == "method_not_allowed"

    def test_a_typo_in_a_command_says_what_is_known(self, hub: Hub) -> None:
        status, error = hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toogle")
        assert status == 404
        assert "Toggle" in error["message"]

    def test_a_value_out_of_range_is_400(self, hub: Hub) -> None:
        status, error = hub.call(
            "PUT", "/nodes/kitchen-relay/ep/4/attributes/Level", {"value": 300}
        )
        assert status == 400
        assert error["problem"] == "invalid"

    def test_broken_json_does_not_bring_the_server_down(self, hub: Hub) -> None:
        connection = http.client.HTTPConnection(*hub.server.address, timeout=5)
        connection.request(
            "POST",
            "/nodes/kitchen-relay/ep/4/commands/Toggle",
            body=b"{this is not json",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        assert connection.getresponse().status == 400
        connection.close()
        assert hub.get("/nodes")[0] == 200, "the server lives on"


class TestControl:
    def test_a_command_reaches_the_node(self, hub: Hub) -> None:
        assert hub.relay.endpoint(4, Output).values[out.ON_OFF] is False
        status, result = hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
        assert status == 200
        assert result["ok"] is True and result["result"] == "OK"
        assert hub.relay.endpoint(4, Output).values[out.ON_OFF] is True

    def test_a_setting_reaches_the_node(self, hub: Hub) -> None:
        status, _ = hub.call(
            "PUT", "/nodes/kitchen-relay/ep/4/attributes/PowerOnBehaviour", {"value": "RESTORE"}
        )
        assert status == 200
        assert hub.relay.endpoint(4, Output).values[out.POWER_ON_BEHAVIOUR] == 2

    def test_a_read_goes_to_the_node(self, hub: Hub) -> None:
        hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
        status, result = hub.get("/nodes/kitchen-relay/ep/4/attributes/OnOff")
        assert status == 200 and result["data"] == "01"

    def test_a_silent_node_is_504_not_500(self, hub: Hub) -> None:
        """504 means "the node did not answer", 500 would mean "the hub fell over"."""
        hub.relay.faults.drop_responses = 9
        status, error = hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
        assert status == 504
        assert error["problem"] == "silent"

    def test_a_refusal_from_a_node_is_409(self, hub: Hub) -> None:
        hub.relay.faults.busy_responses = 9
        status, error = hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
        assert status == 409
        assert error["result"] == "BUSY"


class TestBuses:
    """`/buses` — the hardware under each bus from the file (zadania/FEAT-01.md)."""

    def test_the_list(self, hub: Hub) -> None:
        status, content = hub.get("/buses")
        assert status == 200
        (bus,) = (_build(BusSetupView, item) for item in content)
        assert (bus.name, bus.kind, bus.channel, bus.state) == (
            "vcan0",
            "socketcan",
            "vcan0",
            "drift",
        )
        assert bus.changes[0].how == BITRATE.how

    def test_apply(self, hub: Hub) -> None:
        status, content = hub.call("POST", "/buses/socketcan/vcan0/apply")
        assert status == 200
        assert content["state"] == "ok"
        assert hub.driver.applied == [BITRATE]

    def test_an_unknown_bus(self, hub: Hub) -> None:
        status, content = hub.get("/buses/socketcan/attic")
        assert (status, content["problem"]) == (404, "not_found")

    def test_a_kind_without_routes(self, hub: Hub) -> None:
        status, _ = hub.get("/buses/usr-can315/vcan0")
        assert status == 404

    def test_hardware_that_refuses_is_a_bad_gateway(self, hub: Hub) -> None:
        """Neither the request nor the hub is at fault — the third party in between."""
        hub.driver.fails_apply = Rejected("ip link set vcan0 down", "Operation not permitted")
        status, content = hub.call("POST", "/buses/socketcan/vcan0/apply")
        assert (status, content["problem"]) == (502, "hardware")
        assert "Operation not permitted" in content["message"]


class TestLifecycle:
    """Removing, blocking and enumerating again are routed — the logic is
    `test_removal.py`'s."""

    def test_enumerate_is_started(self, hub: Hub) -> None:
        status, view = hub.call("POST", "/nodes/kitchen-relay/enumerate")
        assert status == 200 and view["started"] is True

    def test_read_all_is_started_for_a_node_and_for_an_endpoint(self, hub: Hub) -> None:
        status, view = hub.call("POST", "/nodes/kitchen-relay/read-all")
        assert status == 200 and view["started"] is True and view["pending"] > 0
        status, error = hub.call("POST", "/nodes/kitchen-relay/ep/4/read-all")
        assert status == 409 and error["problem"] == "conflict", "the first one still runs"

        hub.until(lambda: hub.get("/nodes/kitchen-relay")[1]["reading"] is None, "never ended")
        status, view = hub.call("POST", "/nodes/kitchen-relay/ep/4/read-all")
        assert status == 200 and view["pending"] > 0

    def test_a_blocked_node_shows_it_and_refuses_commands(self, hub: Hub) -> None:
        assert hub.call("POST", "/nodes/kitchen-relay/block")[0] == 200
        assert hub.get("/nodes/kitchen-relay")[1]["state"] == "BLOCKED"
        assert hub.get("/nodes/kitchen-relay")[1]["blocked"] is True
        status, error = hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
        assert error["problem"] == "unavailable", status

        assert hub.call("POST", "/nodes/kitchen-relay/unblock")[0] == 200
        assert hub.get("/nodes/kitchen-relay")[1]["blocked"] is False

    def test_remove_forgets_the_node(self, hub: Hub) -> None:
        status, view = hub.call("POST", "/nodes/kitchen-relay/remove", {"force": False})
        assert status == 200, view
        assert view["acknowledged"] is True and view["unconfigured"] is True
        assert hub.get("/nodes/kitchen-relay")[0] == 404


class TestStream:
    def test_a_state_change_arrives_as_an_event(self, hub: Hub) -> None:
        with _sse(hub) as stream:
            hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
            event = _wait_for(stream, "state", "OnOff")
        assert event["name"] == "OnOff"
        assert event["value"] is True
        assert event["uid"] == RELAY_UID.hex()

    def test_the_stream_needs_no_header(self, hub: Hub) -> None:
        """Because `EventSource` in a browser cannot set it."""
        with _sse(hub, in_param=True) as stream:
            hub.call("POST", "/nodes/kitchen-relay/ep/4/commands/Toggle")
            assert _wait_for(stream, "state", "OnOff")["value"] is True

    def test_the_stream_without_a_token_gets_401(self, hub: Hub) -> None:
        connection = http.client.HTTPConnection(*hub.server.address, timeout=5)
        connection.request("GET", "/events")
        assert connection.getresponse().status == 401
        connection.close()

    def test_a_client_disconnect_is_not_an_error(self, hub: Hub) -> None:
        """A closed browser tab and leaving the full-screen view look the same.

        Before this was fixed, every such disconnection printed a full stack trace to the
        hub's console — that is, it taught whoever watched to ignore their own log.
        """
        with _sse(hub):
            pass
        hub.until(lambda: hub.server.streams == 0, "the stream did not close")
        assert hub.server.errors == 0
        assert hub.get("/nodes")[0] == 200, "the server lives on"


# --- helpers ---------------------------------------------------------------------


class _Sse:
    def __init__(self, hub: Hub, in_param: bool) -> None:
        self.connection = http.client.HTTPConnection(*hub.server.address, timeout=5)
        path = f"/events?token={TOKEN}" if in_param else "/events"
        headers = {} if in_param else {"Authorization": f"Bearer {TOKEN}"}
        self.connection.request("GET", path, headers=headers)
        self.response = self.connection.getresponse()
        assert self.response.status == 200
        assert self.response.getheader("Content-Type") == "text/event-stream"

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> _Sse:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def readline(self) -> str:
        assert self.response.fp is not None
        return self.response.fp.readline().decode("utf-8")


def _sse(hub: Hub, in_param: bool = False) -> _Sse:
    stream = _Sse(hub, in_param)
    assert stream.readline().startswith(": ")  # the greeting
    return stream


def _wait_for(stream: _Sse, kind: str, name: str = "", timeout: float = 5.0) -> Any:
    """The first event of a given kind — and optionally with a given name.

    The name is sometimes needed, because the hub announces its own writes while
    reconciling too: a test that takes "the first `state` that arrives" sometimes lands on
    somebody else's.
    """
    end = time.monotonic() + timeout
    latest = ""
    while time.monotonic() < end:
        line = stream.readline().rstrip("\n")
        if line.startswith("event: "):
            latest = line[7:]
        elif line.startswith("data: ") and latest == kind:
            event = json.loads(line[6:])
            if not name or event["name"] == name:
                return event
    raise AssertionError(f"waited in vain for the event {kind} {name}".strip())
