"""The hub's command line."""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from lagunhub import __version__, doctor, scaffold
from lagunhub.adapters.client import discover
from lagunhub.adapters.client.client import TOKEN_ENV
from lagunhub.adapters.http import HubServer
from lagunhub.adapters.http.auth import TOKEN_FILE, TokenError
from lagunhub.api import ApiError, HubApi
from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config import CONFIG_ENV, ApiConfig, ConfigError, default_config, load
from lagunhub.core.config.apply import apply
from lagunhub.core.config.store import ConfigStore
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import Installation, Snapshot
from lagunhub.core.rx import ColdItem, Received, RxThread, Sent, Task
from lagunhub.monitor import format_frame, format_installation
from lagunhub.protocol import registry
from lagunhub.protocol.bus import SocketCanBus, UdpBus
from lagunhub.protocol.bus.base import Bus
from lagunhub.protocol.bus.udp import DEFAULT_GROUP, DEFAULT_PORT, parse_address

UDP_DEFAULT = f"{DEFAULT_GROUP}:{DEFAULT_PORT}"


def _print_registry() -> None:
    print("Message kinds — the number IS the priority")
    for kind in registry.MessageKind:
        marker = "T" if kind in registry.TRANSACTIONAL else " "
        print(f"  0x{kind.value:X}  {marker}  {kind.name}")

    print("\nResult codes")
    for code in registry.ResultCode:
        marker = "retryable" if code in registry.RETRYABLE else ""
        print(f"  0x{code.value:02X}     {code.name:<22} {marker}")

    print("\nNode classes and address blocks")
    for cls, (lo, hi) in registry.NODE_CLASS_BLOCKS.items():
        print(f"  {cls.value}  {cls.name:<12} 0x{lo:03X}–0x{hi:03X}")

    print("\nSystem endpoints")
    for number, endpoint_type in registry.SYSTEM_ENDPOINTS.items():
        print(f"  ep{number}  {endpoint_type.name}")

    print("\nEndpoint types")
    for endpoint_type in registry.EndpointType:
        revision = registry.TYPE_REVISION.get(endpoint_type)
        state = f"rev. {revision}" if revision else "reserved"
        print(f"  0x{endpoint_type.value:04X}  {endpoint_type.name:<14} {state}")

    print("\nCommon attributes")
    for attribute in registry.CommonAttribute:
        type_name = registry.COMMON_ATTRIBUTE_TYPE[attribute]
        access = registry.COMMON_ATTRIBUTE_ACCESS[attribute]
        print(f"  0x{attribute.value:02X}    {attribute.name:<18} {type_name:<6} {access}")


def _reload(core: CoreThread, where: Path) -> None:
    """A reload that failed **leaves the previous configuration in force**.

    A bad configuration has no right to stop a working installation
    ([`KONFIGURACJA.md`](KONFIGURACJA.md) §11.4).
    """
    try:
        config = load(where)
    except (ConfigError, OSError) as exc:
        core.installation.issues.append(f"reload rejected: {exc}")
        print(
            f"\n!! configuration rejected, the previous one stays in force:\n   {exc}\n", flush=True
        )
        return
    core.submit(lambda: core.apply_config(config))
    print(f"\n>> configuration reloaded from {where}\n", flush=True)


def _serve(
    buses: list[Bus],
    installation: Installation,
    where: Path | None = None,
    api_config: ApiConfig | None = None,
    config_store: ConfigStore | None = None,
    feed: bool | None = None,
    accept_new: bool = False,
) -> int:
    """Start the hub and show the traffic. The threads work alongside and do not wait for
    the printing.

    `feed=None` means **"show it if a human is watching"**: when the output goes to a file
    or through a pipe, five hundred frames a second written round and round is not a log
    but its destruction.
    """
    if feed is None:
        feed = sys.stdout.isatty()
    cold: queue.Queue[ColdItem] = queue.Queue(maxsize=8192)
    display: queue.Queue[Received | Sent] = queue.Queue(maxsize=2048)
    dropped = 0

    def tap(item: ColdItem) -> None:
        nonlocal dropped
        if isinstance(item, Task):
            return  # adapter commands are not traffic on the bus
        try:
            display.put_nowait(item)
        except queue.Full:
            dropped += 1

    rx = RxThread(buses, BindingEngine(buses, installation.snapshot()), cold)
    rx.engine.on_sent = lambda bus, frame: rx.offer(Sent(bus, frame, time.monotonic()))

    def publish(snapshot: Snapshot) -> None:
        rx.engine.snapshot = snapshot

    engine = rx.engine
    core = CoreThread(
        installation, cold, publish, buses=buses, observer=tap, config_store=config_store
    )
    if where is not None:
        signal.signal(signal.SIGHUP, lambda *_: _reload(core, where))

    rx.start()
    core.start()
    if accept_new:
        # On the CORE thread, like every change to the model. It expires by itself,
        # as when opened from the full-screen view.
        core.submit(core.commissioning.accept_new)
        print("accepting new nodes — the mode closes by itself after 15 min", flush=True)
    server = _start_api(core, engine, api_config, where)

    # `Ctrl-C` is SIGINT, but `kill` from a second terminal and closing a `tmux` session
    # are SIGTERM. Without this the hub dies halfway through a write to SQLite, and at
    # the next start it is missing what it had already found out.
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    started = time.monotonic()
    recent = started
    try:
        while not stop.is_set():
            try:
                item = display.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item is not None and feed:
                marker = "→" if isinstance(item, Sent) else " "
                line = format_frame(item.frame, time.monotonic() - started, buses[item.bus].name)
                print(f"{marker} {line}", flush=True)
            if feed and installation.bindings and time.monotonic() - recent >= 10.0:
                recent = time.monotonic()
                print(f"           bindings: {engine.latency.summary()}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping…", flush=True)
        if server is not None:
            server.stop()
        rx.stop()
        core.stop()
        if installation.bindings:
            print(f"\nbinding match: {engine.latency.summary()}")
            print(
                f"matched: {engine.matched}  unbound: {engine.unmatched}  "
                f"sent: {engine.sent}  dropped: {engine.dropped}"
            )
        tx = core.transactions
        print(
            f"CORE: handled {core.handled}  ignored {core.ignored}  "
            f"snapshots {core.published}  the preview lost {dropped}"
        )
        print(
            f"transactions: opened {tx.opened}  answered {tx.answered}  "
            f"retried {tx.retried}  unanswered {tx.lost}  "
            f"unmatched responses {core.unmatched_responses}"
        )
        rec = core.reconcile
        print(
            f"reconcile: checked {rec.checked}  drifted {rec.mismatched}  "
            f"written {rec.written}  failed {rec.failed}"
        )
        if installation.issues:
            print("\nIssues needing the operator's attention:")
            for issue in installation.issues:
                print(f"  • {issue}")
        print()
        print(format_installation(installation, time.monotonic()))
        for bus in buses:
            bus.close()
    return 0


def _init(directory: Path, bus: str, bitrate: int) -> int:
    """A clean configuration **outside the clone** — `config/` in the repository is an
    example to read, not a starting point for a house."""
    directory = directory.expanduser()
    try:
        created = scaffold.init(directory, channel=bus, bitrate=bitrate)
    except scaffold.InitError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    for file_ in created:
        print(f"  created  {file_}")
    print(scaffold.advice(directory))
    return 0


def _doctor(config: Path) -> int:
    """The hub is built on macOS and works on an RPi — so the answer to "what is wrong"
    has to come **from the RPi**, not from guesswork."""
    results = doctor.check(config)
    print(doctor.report(results))
    sentence, code = doctor.summary(results)
    print(f"\n{sentence}")
    return code


def _tui(config: Path, url: str | None, token: str | None) -> int:
    """The full-screen view does not start a hub — it **attaches to a running one**.

    If it started its own, it would be a second hub on the same bus: two address
    assignments, two reconciliations, two truths
    ([`ARCHITEKTURA.md`](ARCHITEKTURA.md) §10.3).
    """
    try:
        conn = discover(config, url, token)
        conn.info()
    except ApiError as exc:
        print(f"cannot connect: {exc.message}", file=sys.stderr)
        return 2
    from lagunhub.adapters.tui import run as tui_run

    return tui_run(conn)


def _start_api(
    core: CoreThread,
    engine: BindingEngine,
    config: ApiConfig | None,
    where: Path | None,
) -> HubServer | None:
    """The server is **on by default**, because without it the hub has no way of being
    operated."""
    if config is None or not config.enabled:
        return None
    api = HubApi(core, engine=engine)
    try:
        server = HubServer(
            api,
            host=config.host,
            port=config.port,
            token=config.token,
            config_dir=where if where is not None and where.is_dir() else None,
        )
    except (OSError, TokenError) as exc:
        # A hub without a server still drives the installation — bindings work in RX.
        # Better that than not coming up at all, as long as what is missing is visible.
        installation_issue = f"the API server did not come up: {exc}"
        core.installation.issues.append(installation_issue)
        print(f"!! {installation_issue}", file=sys.stderr, flush=True)
        return None
    server.start()
    print(f"API on {server.url}", flush=True)
    if server.token_is_new:
        print(f"   token (saved in {where}/{TOKEN_FILE}): {server.token}", flush=True)
    elif config.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"   listening on {config.host} — the token is the only protection", flush=True)
    return server


def _open_buses(channels: list[str], udp: str | None) -> list[Bus]:
    """SocketCAN for the channels, or ONE bus over UDP multicast with `--udp`.

    `--udp` is for running the hub with simulated nodes (`lagun-sim`) — on macOS it is
    the only way to run the hub at all. It is a switch on the command line and not a
    kind of bus in `hub.yaml`, on purpose: a house configuration cannot point at it by
    mistake (zadania/zrobione/FEAT-02.md, N2a). Raises `ValueError` / `OSError` / `RuntimeError`
    with a message fit for a person.
    """
    if udp is not None:
        group, port = parse_address(udp)
        return [UdpBus(group, port)]
    return [SocketCanBus(channel) for channel in channels]


def _ephemeral_config(source: Path | None) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """A configuration in a temporary directory, removed when the hub stops.

    With a source it is a COPY — the hub writes to its configuration (configuration
    writes through the API go to the file), and a trial run must not leave traces in a real one.
    Without a source it is a clean one from `init`. Raises `scaffold.InitError` or
    `OSError`.
    """
    temporary = tempfile.TemporaryDirectory(prefix="lagun-hub-")
    root = Path(temporary.name)
    try:
        if source is None:
            where = root / "config"
            scaffold.init(where, channel="vcan0")
        elif source.is_dir():
            where = root / "config"
            shutil.copytree(source, where, ignore=shutil.ignore_patterns("*.tmp"))
        else:
            where = root / source.name
            shutil.copy2(source, where)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, where


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lagun-hub", description="A LagunBus installation hub")
    parser.add_argument("--version", action="version", version=f"lagun-hub {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("registry", help="show the identifier registry as the hub sees it")

    init_cmd = sub.add_parser("init", help="set up a clean configuration in the given directory")
    init_cmd.add_argument(
        "directory", type=Path, help="e.g. ~/lagun — OUTSIDE the repository clone"
    )
    init_cmd.add_argument("--bus", default="can0", help="the interface name; vcan0 for trials")
    init_cmd.add_argument("--bitrate", type=int, default=500_000)

    check_cmd = sub.add_parser("doctor", help="what is ready on this machine and what is not")
    check_cmd.add_argument("--config", type=Path, default=default_config())

    monitor = sub.add_parser("monitor", help="a traffic preview; it carries out no bindings")
    monitor_source = monitor.add_mutually_exclusive_group()
    monitor_source.add_argument(
        "--bus", action="append", default=None, help="may be given several times"
    )
    monitor_source.add_argument(
        "--udp",
        nargs="?",
        const=UDP_DEFAULT,
        default=None,
        metavar="GROUP[:PORT]",
        help=f"watch the UDP bus of simulated nodes instead (default {UDP_DEFAULT})",
    )

    tui = sub.add_parser("tui", help="the full-screen view: arrows instead of typing")
    tui.add_argument("--config", type=Path, default=default_config())
    tui.add_argument("--url", default=None, help="by default from config/hub.yaml")
    tui.add_argument("--token", default=None, help=f"by default from ${TOKEN_ENV} or from a file")

    run = sub.add_parser("run", help="start the hub with a configuration")
    # `None` means "not given", which `--ephemeral` needs to tell apart from the default.
    run.add_argument("--config", type=Path, default=None, help="by default ./config")
    run_source = run.add_mutually_exclusive_group()
    run_source.add_argument(
        "--bus", action="append", default=None, help="overrides buses from the file"
    )
    run_source.add_argument(
        "--udp",
        nargs="?",
        const=UDP_DEFAULT,
        default=None,
        metavar="GROUP[:PORT]",
        help="TEST ONLY: one bus over UDP multicast on this machine instead of CAN — for "
        f"simulated nodes (lagun-sim); default {UDP_DEFAULT}",
    )
    run.add_argument(
        "--ephemeral",
        action="store_true",
        help="work on a temporary copy of the configuration (a clean one without --config), "
        "removed on exit",
    )
    run.add_argument(
        "--accept-new",
        action="store_true",
        help="open the accepting mode at start, as `accept_new: true` in hub.yaml",
    )
    preview = run.add_mutually_exclusive_group()
    preview.add_argument(
        "--feed",
        dest="feed",
        action="store_true",
        default=None,
        help="print the traffic on the bus; by default only on a terminal",
    )
    preview.add_argument(
        "--quiet", dest="feed", action="store_false", help="do not print the traffic"
    )

    args = parser.parse_args(argv)

    if args.command == "registry":
        _print_registry()
        return 0

    if args.command == "init":
        return _init(args.directory, args.bus, args.bitrate)

    if args.command == "doctor":
        return _doctor(args.config)

    if args.command == "tui":
        return _tui(args.config, args.url, args.token)

    if args.command == "monitor":
        try:
            buses: list[Bus] = _open_buses(args.bus or ["vcan0"], args.udp)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"cannot open the bus: {exc}", file=sys.stderr)
            return 2
        return _serve(buses, Installation())  # the preview exposes no API

    temporary: tempfile.TemporaryDirectory[str] | None = None
    where: Path = args.config if args.config is not None else default_config()
    if args.ephemeral:
        # A source only when one was named — on the command line or in the variable.
        named = args.config is not None or bool(os.environ.get(CONFIG_ENV))
        try:
            temporary, where = _ephemeral_config(where if named else None)
        except (scaffold.InitError, OSError) as exc:
            print(f"cannot prepare a temporary configuration: {exc}", file=sys.stderr)
            return 2
        origin = f"a copy of {args.config or os.environ[CONFIG_ENV]}" if named else "clean"
        print(f"temporary configuration ({origin}), removed on exit: {where}", flush=True)
        print(f"   the view:  lagun-hub tui --config {where}", flush=True)
    try:
        return _run(args, where)
    finally:
        if temporary is not None:
            temporary.cleanup()


def _run(args: argparse.Namespace, where: Path) -> int:
    try:
        config = load(where)
    except ConfigError as exc:
        print(f"the configuration is not valid: {exc}", file=sys.stderr)
        return 2

    try:
        buses = _open_buses(args.bus or list(config.channels), args.udp)
    except (ValueError, OSError, RuntimeError) as exc:
        hint = "" if args.udp else " — without CAN on this machine, try --udp"
        print(f"cannot open the bus: {exc}{hint}", file=sys.stderr)
        return 2
    if args.udp:
        print(
            f"bus {buses[0].name} (UDP {args.udp}) · TEST ONLY — simulated nodes, "
            "the buses from the file are not used",
            flush=True,
        )
    else:
        for bus in config.buses:
            print(f"bus {bus.name} ({bus.channel}) · {bus.bitrate // 1000} kbit/s", flush=True)
    print(f"bindings: {len(config.bindings)}  nodes in the configuration: {len(config.nodes)}")
    for node in config.nodes:
        print(f"  {node.alias:<18} {node.name or node.uid.hex()}", flush=True)
    print("SIGHUP reloads the configuration", flush=True)

    installation = Installation()
    apply(installation, config)
    store = ConfigStore(where)
    return _serve(
        buses,
        installation,
        where,
        config.api,
        store,
        args.feed,
        accept_new=config.accept_new or args.accept_new,
    )


if __name__ == "__main__":
    raise SystemExit(main())
