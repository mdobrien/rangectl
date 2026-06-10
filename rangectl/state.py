from __future__ import annotations
import logging
import sqlite3
import threading
from pathlib import Path

from rangectl import subnet_registry

log = logging.getLogger(__name__)

DB_PATH = Path("~/.rangectl/rangectl.db").expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS topologies (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mgmt_subnet TEXT NOT NULL,
    mgmt_bridge TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_name TEXT NOT NULL REFERENCES topologies(name),
    name TEXT NOT NULL,
    vm_id TEXT,
    image TEXT NOT NULL,
    vcpu INTEGER NOT NULL,
    memory_mb INTEGER NOT NULL,
    os_type TEXT NOT NULL,
    state TEXT NOT NULL,
    mgmt_ip TEXT,
    overlay_path TEXT,
    UNIQUE(topology_name, name)
);

CREATE TABLE IF NOT EXISTS bridges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_name TEXT NOT NULL REFERENCES topologies(name),
    name TEXT NOT NULL,
    subnet TEXT,
    bridge_type TEXT NOT NULL,  -- 'mgmt' or 'topology'
    vlan_aware INTEGER NOT NULL DEFAULT 0,  -- 802.1Q switch (Phase 25)
    -- Namespace-scoped bridge names (data-0, mgmt-br) repeat across ranges,
    -- so uniqueness is per-topology, not global. Legacy hashed names are
    -- globally unique anyway, so this is strictly looser.
    UNIQUE(topology_name, name)
);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_name TEXT NOT NULL REFERENCES topologies(name),
    node_a TEXT NOT NULL,
    iface_a TEXT NOT NULL,
    ip_a TEXT,
    node_b TEXT NOT NULL,
    iface_b TEXT NOT NULL,
    ip_b TEXT,
    bridge_name TEXT,
    is_up BOOLEAN DEFAULT 1,
    -- Per-side 802.1Q port config as JSON (Phase 25):
    -- {"mode": "access"|"trunk", "vids": [...], "native": int|null}
    vlan_a TEXT,
    vlan_b TEXT
);

CREATE TABLE IF NOT EXISTS mgmt_subnets (
    subnet TEXT PRIMARY KEY,
    topology_name TEXT,
    allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_name TEXT NOT NULL REFERENCES topologies(name),
    node_name TEXT,  -- NULL = topology-wide
    snapshot_name TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS images (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    inject TEXT NOT NULL DEFAULT 'pre-baked',
    os_type TEXT NOT NULL DEFAULT 'linux',
    size_mb INTEGER,
    built_from TEXT,  -- base image name if created by ImageBuilder
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_name TEXT NOT NULL,
    node_name TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class StateDB:

    def __init__(self, db_path: str | Path | None = None,
                 subnet_registry: str | Path | None = None) -> None:
        if db_path == ":memory:":
            self._path = ":memory:"
        else:
            path = Path(db_path) if db_path else DB_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(path)
        # Host-global mgmt-subnet allocator path (None -> env/default resolved at
        # call time). Subnet allocation is per-HOST, not per-DB, so independent
        # StateDBs never hand out the same /24. See subnet_registry.py.
        self._subnet_registry = subnet_registry
        log.info("StateDB opening at %s", self._path)
        # check_same_thread=False so wave-parallel deploys can write from worker
        # threads. The lock below serializes access — sqlite itself isn't safe
        # to share without it.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS
    # never alters an existing table, so each is back-filled with ALTER TABLE
    # (a duplicate-column error means the column is already there).
    _MIGRATIONS = [
        ("bridges", "vlan_aware", "INTEGER NOT NULL DEFAULT 0"),
        ("links", "vlan_a", "TEXT"),
        ("links", "vlan_b", "TEXT"),
    ]

    def _init_schema(self) -> None:
        log.info("Initializing DB schema")
        self._conn.executescript(SCHEMA)
        for table, column, decl in self._MIGRATIONS:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass  # column already present (fresh schema or migrated)
        self._conn.commit()

    def allocate_mgmt_subnet(self, topology_name: str) -> str:
        log.info("Allocating mgmt subnet for topology '%s'", topology_name)
        # Authoritative pick comes from the host-global flock registry so
        # concurrent ranges (separate DBs/processes) never collide. Mirror the
        # result into the local table for inspection/persistence.
        candidate = subnet_registry.allocate(topology_name, self._subnet_registry)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO mgmt_subnets (subnet, topology_name) "
                "VALUES (?, ?)",
                (candidate, topology_name),
            )
            self._conn.commit()
        return candidate

    def free_mgmt_subnet(self, topology_name: str) -> None:
        log.info("Freeing mgmt subnet for topology '%s'", topology_name)
        subnet_registry.free(topology_name, self._subnet_registry)
        with self._lock:
            self._conn.execute(
                "DELETE FROM mgmt_subnets WHERE topology_name=?", (topology_name,)
            )
            self._conn.commit()

    def save_topology(self, name: str, status: str, mgmt_subnet: str, mgmt_bridge: str) -> None:
        log.info("Saving topology '%s' (status=%s)", name, status)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO topologies (name, status, mgmt_subnet, mgmt_bridge) "
                "VALUES (?, ?, ?, ?)",
                (name, status, mgmt_subnet, mgmt_bridge),
            )
            self._conn.commit()

    def save_node(self, topology_name: str, name: str, image: str, vcpu: int,
                  memory_mb: int, os_type: str, state: str,
                  mgmt_ip: str | None = None, vm_id: str | None = None) -> None:
        log.info("Saving node '%s/%s' (state=%s)", topology_name, name, state)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes "
                "(topology_name, name, vm_id, image, vcpu, memory_mb, os_type, state, mgmt_ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (topology_name, name, vm_id, image, vcpu, memory_mb, os_type, state, mgmt_ip),
            )
            self._conn.commit()

    def update_node_state(self, topology_name: str, name: str, state: str) -> None:
        log.info("Updating node state '%s/%s' -> %s", topology_name, name, state)
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET state=? WHERE topology_name=? AND name=?",
                (state, topology_name, name),
            )
            self._conn.commit()

    def log_event(self, topology_name: str, node_name: str | None,
                  level: str, message: str) -> None:
        log.info("[%s/%s] %s: %s", topology_name, node_name or "*", level, message)
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (topology_name, node_name, level, message) VALUES (?, ?, ?, ?)",
                (topology_name, node_name, level, message),
            )
            self._conn.commit()

    def get_logs(self, topology_name: str, node_name: str | None = None,
                 level: str | None = None) -> list[dict]:
        log.info("Fetching logs for %s/%s (level=%s)", topology_name, node_name, level)
        query = "SELECT * FROM logs WHERE topology_name=?"
        params: list = [topology_name]
        if node_name is not None:
            query += " AND node_name=?"
            params.append(node_name)
        if level is not None:
            query += " AND level=?"
            params.append(level)
        query += " ORDER BY id ASC"
        with self._lock:
            cur = self._conn.execute(query, params)
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def list_nodes(self, topology_name: str) -> list[dict]:
        log.info("Listing nodes for topology '%s'", topology_name)
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM nodes WHERE topology_name=? ORDER BY id ASC",
                (topology_name,),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def list_bridges(self, topology_name: str) -> list[dict]:
        log.info("Listing bridges for topology '%s'", topology_name)
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM bridges WHERE topology_name=? ORDER BY id ASC",
                (topology_name,),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def list_links(self, topology_name: str) -> list[dict]:
        log.info("Listing links for topology '%s'", topology_name)
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM links WHERE topology_name=? ORDER BY id ASC",
                (topology_name,),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def get_topology(self, name: str) -> dict | None:
        log.info("Getting topology '%s'", name)
        with self._lock:
            cur = self._conn.execute("SELECT * FROM topologies WHERE name=?", (name,))
            row = cur.fetchone()
            return _row_to_dict(cur, row) if row else None

    def list_topologies(self) -> list[dict]:
        log.info("Listing all topologies")
        with self._lock:
            cur = self._conn.execute("SELECT * FROM topologies ORDER BY name")
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def delete_topology(self, name: str) -> None:
        log.info("Deleting topology '%s' from DB", name)
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE topology_name=?", (name,))
            self._conn.execute("DELETE FROM bridges WHERE topology_name=?", (name,))
            self._conn.execute("DELETE FROM links WHERE topology_name=?", (name,))
            self._conn.execute("DELETE FROM snapshots WHERE topology_name=?", (name,))
            self._conn.execute("DELETE FROM mgmt_subnets WHERE topology_name=?", (name,))
            self._conn.execute("DELETE FROM topologies WHERE name=?", (name,))
            self._conn.commit()

    def add_image(self, name: str, path: str, inject: str = "pre-baked",
                  os_type: str = "linux", size_mb: int | None = None,
                  built_from: str | None = None) -> None:
        log.info("Adding image '%s' (path=%s, inject=%s)", name, path, inject)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO images "
                "(name, path, inject, os_type, size_mb, built_from) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, path, inject, os_type, size_mb, built_from),
            )
            self._conn.commit()

    def remove_image(self, name: str) -> None:
        log.info("Removing image '%s'", name)
        with self._lock:
            self._conn.execute("DELETE FROM images WHERE name=?", (name,))
            self._conn.commit()

    def get_image(self, name: str) -> dict | None:
        log.info("Getting image '%s'", name)
        with self._lock:
            cur = self._conn.execute("SELECT * FROM images WHERE name=?", (name,))
            row = cur.fetchone()
            return _row_to_dict(cur, row) if row else None

    def list_images(self) -> list[dict]:
        log.info("Listing all images")
        with self._lock:
            cur = self._conn.execute("SELECT * FROM images ORDER BY name")
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def image_exists(self, name: str) -> bool:
        log.info("Checking image exists: '%s'", name)
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM images WHERE name=?", (name,))
            return cur.fetchone() is not None

    def close(self) -> None:
        self._conn.close()
