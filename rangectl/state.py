from __future__ import annotations
import logging
import sqlite3
from pathlib import Path

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
    topology_name TEXT REFERENCES topologies(name),
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


class StateDB:

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        log.info("StateDB opening at %s", self._path)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        log.info("Initializing DB schema")
        self._conn.executescript(SCHEMA)

    def allocate_mgmt_subnet(self, topology_name: str) -> str:
        log.info("Allocating mgmt subnet for topology '%s'", topology_name)
        # find next available /24 from 192.168.100.0/24 pool
        raise NotImplementedError

    def free_mgmt_subnet(self, topology_name: str) -> None:
        log.info("Freeing mgmt subnet for topology '%s'", topology_name)
        raise NotImplementedError

    def save_topology(self, name: str, status: str, mgmt_subnet: str, mgmt_bridge: str) -> None:
        log.info("Saving topology '%s' (status=%s)", name, status)
        raise NotImplementedError

    def save_node(self, topology_name: str, name: str, image: str, vcpu: int,
                  memory_mb: int, os_type: str, state: str, mgmt_ip: str | None = None) -> None:
        log.info("Saving node '%s/%s' (state=%s)", topology_name, name, state)
        raise NotImplementedError

    def update_node_state(self, topology_name: str, name: str, state: str) -> None:
        log.info("Updating node state '%s/%s' -> %s", topology_name, name, state)
        raise NotImplementedError

    def log_event(self, topology_name: str, node_name: str | None,
                  level: str, message: str) -> None:
        log.info("[%s/%s] %s: %s", topology_name, node_name or "*", level, message)
        raise NotImplementedError

    def get_logs(self, topology_name: str, node_name: str | None = None,
                 level: str | None = None) -> list[dict]:
        log.info("Fetching logs for %s/%s (level=%s)", topology_name, node_name, level)
        raise NotImplementedError

    def get_topology(self, name: str) -> dict | None:
        log.info("Getting topology '%s'", name)
        raise NotImplementedError

    def list_topologies(self) -> list[dict]:
        log.info("Listing all topologies")
        raise NotImplementedError

    def delete_topology(self, name: str) -> None:
        log.info("Deleting topology '%s' from DB", name)
        raise NotImplementedError

    def add_image(self, name: str, path: str, inject: str = "pre-baked",
                  os_type: str = "linux", size_mb: int | None = None,
                  built_from: str | None = None) -> None:
        log.info("Adding image '%s' (path=%s, inject=%s)", name, path, inject)
        raise NotImplementedError

    def remove_image(self, name: str) -> None:
        log.info("Removing image '%s'", name)
        raise NotImplementedError

    def get_image(self, name: str) -> dict | None:
        log.info("Getting image '%s'", name)
        raise NotImplementedError

    def list_images(self) -> list[dict]:
        log.info("Listing all images")
        raise NotImplementedError

    def image_exists(self, name: str) -> bool:
        log.info("Checking image exists: '%s'", name)
        raise NotImplementedError

    def close(self) -> None:
        self._conn.close()
