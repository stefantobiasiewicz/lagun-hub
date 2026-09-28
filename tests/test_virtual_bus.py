"""VirtualBus — it has to fake a bus faithfully enough for the tests to mean
something."""

from __future__ import annotations

import selectors
import threading

from lagunhub.protocol.bus import Bus, VirtualNetwork
from lagunhub.protocol.frame import Frame, encode_id


def event(address: int) -> Frame:
    return Frame(encode_id(0x1, address, 4, 0x03), b"\x00\x01")


def report(address: int) -> Frame:
    return Frame(encode_id(0x8, address, 4, 0x00), b"\x01")


def test_it_satisfies_the_interface() -> None:
    net = VirtualNetwork()
    assert isinstance(net.attach("can0"), Bus)


def test_delivery_to_the_remaining_ports() -> None:
    net = VirtualNetwork()
    a, b, c = net.attach("a"), net.attach("b"), net.attach("c")
    f = event(0x02A)
    a.send(f)
    assert b.recv() == f
    assert c.recv() == f
    assert a.recv() is None, "a sender does not hear itself"


def test_an_empty_bus_does_not_block() -> None:
    net = VirtualNetwork()
    assert net.attach("a").recv() is None


def test_the_lower_identifier_wins() -> None:
    """With several frames ready at once the one with the lower identifier goes out."""
    net = VirtualNetwork(manual=True)
    senders = [net.attach(f"n{i}") for i in range(4)]
    receiver = net.attach("hub")

    # The order of sending is the reverse of the arbitration order.
    senders[0].send(report(0x300))  # kind 0x8 — the lowest priority
    senders[1].send(event(0x2A0))  # kind 0x1
    senders[2].send(event(0x010))  # kind 0x1, a lower address
    senders[3].send(Frame(encode_id(0x0, 0x050, 4, 0x01), b"\x01"))  # ALARM

    net.flush()
    order = []
    while (f := receiver.recv()) is not None:
        order.append(f.can_id)

    assert order == sorted(order)
    assert order[0] >> 25 == 0x0, "an ALARM overtakes everything"
    assert order[-1] >> 25 == 0x8, "a REPORT travels last"


def test_the_same_sender_keeps_its_order() -> None:
    """CAN does not reorder frames with the same identifier
    (05-mapowanie-can.md §6.4)."""
    net = VirtualNetwork(manual=True)
    a, b = net.attach("a"), net.attach("b")
    for i in range(10):
        a.send(Frame(encode_id(0x9, 0x02A, 4, 0x00), bytes([0x80, i])))
    net.flush()
    numbers = []
    while (f := b.recv()) is not None:
        numbers.append(f.data[1])
    assert numbers == list(range(10))


def test_the_descriptor_works_with_select() -> None:
    """The RX thread is not to know what it sits on — select() has to work the same."""
    net = VirtualNetwork()
    a, b = net.attach("a"), net.attach("b")
    sel = selectors.DefaultSelector()
    sel.register(b, selectors.EVENT_READ)

    assert sel.select(timeout=0) == [], "silence on the bus wakes nobody"

    f = event(0x02A)
    a.send(f)
    done = sel.select(timeout=1)
    assert [key.fileobj for key, _ in done] == [b]
    assert b.recv() == f
    assert sel.select(timeout=0) == [], "after receiving, the descriptor goes quiet again"
    sel.close()


def test_several_threads_send_without_losing_frames() -> None:
    net = VirtualNetwork()
    senders = [net.attach(f"n{i}") for i in range(4)]
    receiver = net.attach("hub")
    on_thread = 250

    def send_all(port: object, base: int) -> None:
        for i in range(on_thread):
            port.send(Frame(encode_id(0x1, base, 4, 0x03), bytes([i & 0xFF, 1])))  # type: ignore[attr-defined]

    threads = [
        threading.Thread(target=send_all, args=(port, 0x100 + i)) for i, port in enumerate(senders)
    ]
    for w in threads:
        w.start()
    for w in threads:
        w.join()
    net.flush()

    received = 0
    while receiver.recv() is not None:
        received += 1
    assert received == len(senders) * on_thread


def test_a_closed_port_stops_receiving() -> None:
    net = VirtualNetwork()
    a, b = net.attach("a"), net.attach("b")
    b.close()
    a.send(event(0x02A))  # it must not blow up
    b.close()  # closing twice is safe
