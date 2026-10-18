"""A bus that goes down under a running hub (zadania/zrobione/BUG-04.md).

What the kernel does to a CAN_RAW socket when its interface goes ``down``: ``recv``
fails **once** with ``ENETDOWN`` (the error waits on the socket and wakes ``select``),
and every ``send`` fails with ``ENETDOWN`` for as long as the interface is down. After
``up`` the same socket works again — it is bound to the interface's index, not to its
state. An interface that disappears (``ENODEV``) fails ``recv`` once the same way.

The hub used to die of it: the RX thread on the first ``recv``, or on a binding's
``send``; CORE on a retry's ``send`` — and with CORE the API, which waits on CORE's
queue. Here none of them may.
"""

from __future__ import annotations

import errno
import queue
import threading
import time

import pytest

from lagunhub.api import ApiError, HubApi
from lagunhub.buswatch import BusWatch
from lagunhub.cli import _dead_threads
from lagunhub.core.hub import CoreThread
from lagunhub.core.rx import RxThread
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.bus import Bus, VirtualNetwork
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame
from lagunhub.protocol.registry import MessageKind
from tests.test_e2e import Rig, build


class Faulty:
    """The hub's port with the kernel's behaviour for an interface going ``down``.

    Wraps a `VirtualBus` port; satisfies ``Bus``.
    """

    def __init__(self, inner: Bus, net: VirtualNetwork | None = None) -> None:
        self.inner = inner
        self.net = net
        self.name = inner.name
        self.down = False
        self.pending: int | None = None
        """An error waiting on the socket — the next ``recv`` raises it, once."""

    def go_down(self, code: int = errno.ENETDOWN) -> None:
        self.down = True
        self.pending = code
        # The kernel wakes `select` with the error; here a frame of our own to nobody
        # does the same — it is what `recv` would have returned had the error not been
        # first in line, and the hub must not see it either way.
        self.inner._deliver(Frame(0, b""))  # type: ignore[attr-defined]

    def go_up(self) -> None:
        self.down = False

    def reopen(self) -> None:
        """`SocketCanBus.reopen`: a new socket — a new descriptor — and the old closed."""
        assert self.net is not None
        old, self.inner = self.inner, self.net.attach("hub-reopened")
        old.close()

    def fileno(self) -> int:
        return self.inner.fileno()

    def recv(self) -> Frame | None:
        if self.pending is not None:
            code, self.pending = self.pending, None
            self.inner.recv()  # the wake-up above
            raise OSError(code, errno.errorcode[code])
        return self.inner.recv()

    def send(self, frame: Frame) -> None:
        if self.down:
            raise OSError(errno.ENETDOWN, "Network is down")
        self.inner.send(frame)

    def close(self) -> None:
        self.inner.close()


def rig(tmp_path) -> tuple[Rig, Faulty]:  # type: ignore[no-untyped-def]
    net = VirtualNetwork()
    ports: dict[str, Faulty] = {}

    def attach(name: str) -> Bus:
        port = net.attach(name)
        if name != "hub":
            return port
        ports[name] = Faulty(port, net)
        return ports[name]

    inst = build(tmp_path, attach=attach)
    return inst, ports["hub"]


@pytest.mark.parametrize("code", [errno.ENETDOWN, errno.ENODEV])
def test_reception_survives_the_interface_going_down(tmp_path, code: int) -> None:  # type: ignore[no-untyped-def]
    inst, hub = rig(tmp_path)

    hub.go_down(code)
    inst.run(0.010)
    hub.go_up()

    inst.click()
    assert inst.output.values[out.ON_OFF] is True, "frames after `up` reach the binding"


def test_the_rx_thread_survives_the_interface_going_down(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """On a real `select` — the error is what wakes it."""
    inst, hub = rig(tmp_path)
    inst.rx.start()
    try:
        hub.go_down()
        time.sleep(0.050)
        hub.go_up()
        assert inst.rx.is_alive(), "RX outlived ENETDOWN"

        inst.key.force_click()
        inst.key_sim.flush()
        deadline = time.monotonic() + 1.0
        while inst.engine.matched < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        inst.rx.stop()
    assert inst.engine.matched == 1


def test_the_rx_thread_follows_a_reopened_socket(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An adapter plugged back gets a new socket (zadania/zrobione/BUG-04.md, D3) — RX has to
    `select` on the new descriptor, not on the closed one."""
    inst, hub = rig(tmp_path)
    inst.rx.start()
    try:
        time.sleep(0.050)
        old = hub.fileno()
        hub.reopen()
        assert hub.fileno() != old
        time.sleep(0.300)  # one turn of RX's loop, 0.2 s at most

        inst.key.force_click()
        inst.key_sim.flush()
        deadline = time.monotonic() + 1.0
        while inst.engine.matched < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert inst.rx.is_alive()
    finally:
        inst.rx.stop()
    assert inst.engine.matched == 1


def test_a_binding_sending_onto_a_bus_that_is_down_drops_the_command(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The fast path does not retry — a late light is worse than none."""
    inst, hub = rig(tmp_path)
    hub.down = True  # `send` fails, `recv` still has the key's frames in it

    inst.click()
    assert inst.output.values[out.ON_OFF] is False
    assert inst.engine.matched == 1
    assert inst.engine.sent == 0
    assert inst.engine.dropped == 1

    hub.go_up()
    inst.click()
    assert inst.output.values[out.ON_OFF] is True, "after `up` the next click goes through"


def test_core_sending_onto_a_bus_that_is_down_keeps_the_transaction(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The first attempt is lost on the bus; the retry after `up` gets through."""
    inst, hub = rig(tmp_path)
    hub.down = True

    results: list[Outcome] = []
    inst.core.request(0x040, 4, out.TOGGLE, kind=MessageKind.COMMAND, on_done=results.append)
    inst.run(0.050)
    hub.go_up()
    inst.run(0.150)  # the first repetition falls due after 100 ms

    assert [r.ok for r in results] == [True]
    assert inst.output.values[out.ON_OFF] is True
    assert inst.core.transactions.retried == 1


def test_core_ends_a_transaction_as_lost_when_the_bus_stays_down(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inst, hub = rig(tmp_path)
    hub.down = True

    results: list[Outcome] = []
    inst.core.request(0x040, 4, out.TOGGLE, kind=MessageKind.COMMAND, on_done=results.append)
    inst.run(0.400)

    assert [r.answered for r in results] == [False]
    assert inst.core.transactions.lost == 1


def test_the_core_thread_survives_sending_onto_a_bus_that_is_down(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """On the real thread — CORE dead means the API dead too."""
    inst, hub = rig(tmp_path)
    hub.down = True
    inst.core.start()
    try:
        done: queue.Queue[Outcome] = queue.Queue()
        inst.core.submit(
            lambda: inst.core.request(
                0x040, 4, out.TOGGLE, kind=MessageKind.COMMAND, on_done=done.put
            )
        )
        time.sleep(0.050)
        assert inst.core.is_alive(), "CORE outlived ENETDOWN on send"
    finally:
        inst.core.stop()


KILLED = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
"""`_killed` ends CORE with an exception on purpose."""


def _killed(inst: Rig) -> None:
    """Start CORE and have it die of something other than a bus."""

    def boom() -> None:
        raise RuntimeError("a bug, not a bus")

    inst.core.start()
    inst.core.submit(boom)
    inst.core.join(timeout=1.0)
    assert not inst.core.is_alive()


@KILLED
def test_the_api_says_core_stopped_instead_of_waiting_out_the_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inst, _ = rig(tmp_path)
    _killed(inst)
    api = HubApi(inst.core, timeout=5.0)

    started = time.monotonic()
    with pytest.raises(ApiError, match="CORE thread has stopped"):
        api.health()
    assert time.monotonic() - started < 1.0


@KILLED
def test_a_dead_thread_is_said_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inst, _ = rig(tmp_path)
    reported: set[str] = set()
    assert _dead_threads((inst.rx, inst.core), reported, inst.core) == [], "not started yet"

    _killed(inst)
    assert _dead_threads((inst.rx, inst.core), reported, inst.core) == [
        "the hub's CORE thread has stopped — restart the hub"
    ]
    assert _dead_threads((inst.rx, inst.core), reported, inst.core) == []


def test_a_dead_rx_thread_lands_in_the_issues(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inst, _ = rig(tmp_path)
    inst.rx.start()
    inst.rx.stop()
    inst.rx.join(timeout=1.0)

    lines = _dead_threads((inst.rx, inst.core), set(), inst.core)
    inst.core.pump(inst.clock.now)

    assert lines == ["the hub's RX thread has stopped — restart the hub"]
    assert inst.core.installation.issues[-1] == lines[0]


def test_a_frame_the_bus_cannot_decode_does_not_end_reception(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A `ValueError` from `recv` is the bus's fault, counted against it like `OSError`
    (zadania/zrobione/BUG-10.md, R2) — and only around `recv`: the binding engine's own errors
    still surface."""
    inst, hub = rig(tmp_path)
    real = hub.recv
    broken = [True]

    def recv() -> Frame | None:
        if broken[0]:
            broken[0] = False
            raise ValueError("identifier does not fit into 29 bits: 0x40000123")
        return real()

    hub.recv = recv  # type: ignore[method-assign]
    inst.rx.pump()
    assert inst.rx.errors == [1]

    inst.click()
    assert inst.output.values[out.ON_OFF] is True, "the next frame still reaches the binding"


# Thread internals across the versions `requires-python` allows. 3.12 calls
# `self._stop()` from `join()` and `is_alive()`; 3.13 does not have it any more, so
# `dir(threading.Thread)` alone would not catch it here.
_THREAD_INTERNALS = frozenset(dir(threading.Thread)) | {
    "_stop",
    "_started",
    "_is_stopped",
    "_tstate_lock",
    "_handle",
    "_target",
    "_name",
    "_args",
    "_kwargs",
    "_daemonic",
    "_ident",
    "_native_id",
}


@pytest.mark.parametrize("cls", [RxThread, CoreThread, BusWatch])
def test_no_hub_thread_shadows_what_threading_uses(cls: type[threading.Thread]) -> None:
    """`self._stop = Event()` turned `join()` and `is_alive()` into `TypeError` on 3.12 —
    the very calls that report a dead thread (zadania/zrobione/BUG-10.md, R3)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cls))
    assigned = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    assert not assigned & _THREAD_INTERNALS
