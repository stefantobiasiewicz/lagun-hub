"""The installation model in SQLite.

The write is **whole, not incremental**: at a hundred nodes that is a few dozen rows,
and going incremental would add state to maintain in order to save a millisecond once
an hour. The priorities here are the reverse of the usual — simplicity is worth more
than that millisecond, because the database sits on no time-budgeted path anyway.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lagunhub.core.model import EndpointModel, Installation, JoinRecord, NodeModel, NodeState, Uid
from lagunhub.protocol.registry import EndpointType, NodeClass

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    uid         BLOB PRIMARY KEY,
    short_id    INTEGER NOT NULL,
    address     INTEGER NOT NULL,
    bus         INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    vendor_id   INTEGER NOT NULL,
    device_type INTEGER NOT NULL,
    firmware    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints (
    uid      BLOB    NOT NULL,
    number   INTEGER NOT NULL,
    type_id  INTEGER,
    revision INTEGER NOT NULL,
    features INTEGER NOT NULL,
    channel  INTEGER NOT NULL,
    PRIMARY KEY (uid, number)
);
CREATE TABLE IF NOT EXISTS records (
    short_id   INTEGER PRIMARY KEY,
    node_class INTEGER NOT NULL,
    address    INTEGER NOT NULL,
    bus        INTEGER NOT NULL,
    state      TEXT    NOT NULL,
    uid        BLOB
);
"""


class SqliteStore:
    """Satisfies `Store`. Not thread-safe — it belongs to the STORE thread."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def save(self, installation: Installation) -> None:
        """Stores **only what was read from the bus**.

        There are no names here and there are not to be: a name is a human decision and
        lives in the configuration. Stored here as well, it would be a second source of
        truth — and a hub that lost its files would then show an installation with
        names and without bindings, that is, one **working in appearance only** —
        [`07-hub.md`](../../../lagunbus-spec/07-hub.md) §5.3.
        """
        with self._db:
            self._db.execute("DELETE FROM nodes")
            self._db.execute("DELETE FROM endpoints")
            self._db.execute("DELETE FROM records")
            self._db.executemany(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        bytes(node.uid),
                        node.short_id,
                        node.address,
                        node.bus,
                        node.state.name,
                        node.vendor_id,
                        node.device_type,
                        node.firmware,
                    )
                    for node in installation.nodes.values()
                ],
            )
            self._db.executemany(
                "INSERT INTO endpoints VALUES (?,?,?,?,?,?)",
                [
                    (
                        bytes(node.uid),
                        endpoint.number,
                        int(endpoint.type_id) if endpoint.type_id is not None else None,
                        endpoint.revision,
                        endpoint.features,
                        endpoint.channel,
                    )
                    for node in installation.nodes.values()
                    for endpoint in node.endpoints.values()
                ],
            )
            self._db.executemany(
                "INSERT INTO records VALUES (?,?,?,?,?,?)",
                [
                    (
                        record.short_id,
                        int(record.node_class),
                        record.address,
                        record.bus,
                        record.state.name,
                        bytes(record.uid) if record.uid is not None else None,
                    )
                    for record in installation.records.values()
                ],
            )

    def load(self) -> Installation:
        """Restores the model. **It does not restore attribute state — and must not.**

        One must not assume the outputs are in the state they were before the restart:
        the nodes may have been switched by hand in the meantime, or `AutoOffTime` may
        have fired. The truth is the first report that arrives —
        [`07-hub.md`](../../../lagunbus-spec/07-hub.md) §5.2.
        """
        installation = Installation()
        for row in self._db.execute("SELECT * FROM nodes"):
            uid = Uid(bytes(row[0]))
            installation.add(
                NodeModel(
                    uid=uid,
                    address=row[2],
                    bus=row[3],
                    state=NodeState[row[4]],
                    short_id=row[1],
                    vendor_id=row[5],
                    device_type=row[6],
                    firmware=row[7],
                )
            )
        for row in self._db.execute("SELECT * FROM endpoints"):
            node = installation.nodes.get(Uid(bytes(row[0])))
            if node is None:
                continue
            node.endpoints[row[1]] = EndpointModel(
                number=row[1],
                type_id=EndpointType(row[2]) if row[2] is not None else None,
                revision=row[3],
                features=row[4],
                channel=row[5],
            )
        for row in self._db.execute("SELECT * FROM records"):
            installation.records[row[0]] = JoinRecord(
                short_id=row[0],
                node_class=NodeClass(row[1]),
                address=row[2],
                bus=row[3],
                state=NodeState[row[4]],
                uid=Uid(bytes(row[5])) if row[5] is not None else None,
            )
            installation.addresses.reserve(row[2])
        return installation

    def close(self) -> None:
        self._db.close()
