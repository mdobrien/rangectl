from __future__ import annotations
import ipaddress
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path("~/.rangectl/rangectl.db").expanduser()

MGMT_POOL_BASE = ipaddress.IPv4Network("192.168.100.0/24")
MGMT_POOL_PREFIX = 24
MGMT_POOL_SIZE = 100  # /24s available: 192.168.100.0/24 .. 192.168.199.0/24

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
    name TEXT NOT NULL UNIQUE,
    subnet TEXT,
    bridge_type TEXT NOT NULL  -- 'mgmt' or 'topology'
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
    is_up BOOLEAN DEFAULT 1
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

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path == ":memory:":
            self._path = ":memory:"
        else:
            path = Path(db_path) if db_path else DB_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(path)
        log.info("StateDB opening at %s", self._path)
        self._conn = sqlite3.connect(self._path)
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        log.info("Initializing DB schema")
        self._conn.executescript(SCHEMA)

    def allocate_mgmt_subnet(self, topology_name: str) -> str:
        log.info("Allocating mgmt subnet for topology '%s'", topology_name)
        cur = self._conn.execute("SELECT subnet FROM mgmt_subnets")
        taken = {row[0] for row in cur.fetchall()}
        base = int(MGMT_POOL_BASE.network_address)
        for i in range(MGMT_POOL_SIZE):
            candidate_net = ipaddress.IPv4Network((base + i * 256, MGMT_POOL_PREFIX))
            candidate = f"{candidate_net.network_address}/{MGMT_POOL_PREFIX}"
            if candidate not in taken:
                self._conn.execute(
                    "INSERT INTO mgmt_subnets (subnet, topology_name) VALUES (?, ?)",
                    (candidate, topology_name),
                )
                self._conn.commit()
                return candidate
        raise RuntimeError("mgmt subnet pool exhausted")

    def free_mgmt_subnet(self, topology_name: str) -> None:
        log.info("Freeing mgmt subnet for topology '%s'", topology_name)
        self._conn.execute(
            "DELETE FROM mgmt_subnets WHERE topology_name=?", (topology_name,)
        )
        self._conn.commit()

    def save_topology(self, name: str, status: str, mgmt_subnet: str, mgmt_bridge: str) -> None:
        log.info("Saving topology '%s' (status=%s)", name, status)
        self._conn.execute(
            "INSERT OR REPLACE INTO topologies (name, status, mgmt_subnet, mgmt_bridge) "
            "VALUES (?, ?, ?, ?)",
            (name, status, mgmt_subnet, mgmt_bridge),
        )
        self._conn.commit()

    def save_node(self, topology_name: str, name: str, image: str, vcpu: int,
                  memory_mb: int, os_type: str, state: str, mgmt_ip: str | None = None) -> None:
        log.info("Saving node '%s/%s' (state=%s)", topology_name, name, state)
        self._conn.execute(
            "INSERT OR REPLACE INTO nodes "
            "(topology_name, name, image, vcpu, memory_mb, os_type, state, mgmt_ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topology_name, name, image, vcpu, memory_mb, os_type, state, mgmt_ip),
        )
        self._conn.commit()

    def update_node_state(self, topology_name: str, name: str, state: str) -> None:
        log.info("Updating node state '%s/%s' -> %s", topology_name, name, state)
        self._conn.execute(
            "UPDATE nodes SET state=? WHERE topology_name=? AND name=?",
            (state, topology_name, name),
        )
        self._conn.commit()

    def log_event(self, topology_name: str, node_name: str | None,
                  level: str, message: str) -> None:
        log.info("[%s/%s] %s: %s", topology_name, node_name or "*", level, message)
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
        cur = self._conn.execute(query, params)
        return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def get_topology(self, name: str) -> dict | None:
        log.info("Getting topology '%s'", name)
        cur = self._conn.execute("SELECT * FROM topologies WHERE name=?", (name,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def list_topologies(self) -> list[dict]:
        log.info("Listing all topologies")
        cur = self._conn.execute("SELECT * FROM topologies ORDER BY name")
        return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def delete_topology(self, name: str) -> None:
        log.info("Deleting topology '%s' from DB", name)
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
        self._conn.execute(
            "INSERT OR REPLACE INTO images "
            "(name, path, inject, os_type, size_mb, built_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, path, inject, os_type, size_mb, built_from),
        )
        self._conn.commit()

    def remove_image(self, name: str) -> None:
        log.info("Removing image '%s'", name)
        self._conn.execute("DELETE FROM images WHERE name=?", (name,))
        self._conn.commit()

    def get_image(self, name: str) -> dict | None:
        log.info("Getting image '%s'", name)
        cur = self._conn.execute("SELECT * FROM images WHERE name=?", (name,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def list_images(self) -> list[dict]:
        log.info("Listing all images")
        cur = self._conn.execute("SELECT * FROM images ORDER BY name")
        return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def image_exists(self, name: str) -> bool:
        log.info("Checking image exists: '%s'", name)
        cur = self._conn.execute("SELECT 1 FROM images WHERE name=?", (name,))
        return cur.fetchone() is not None

    def close(self) -> None:
        self._conn.close()
