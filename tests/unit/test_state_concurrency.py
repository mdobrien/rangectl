"""Concurrency tests for StateDB.

StateDB shares one sqlite3 connection across threads (check_same_thread=False).
The lock serializes access; without it, concurrent reads interleaving with
locked writes on the same connection raise sqlite3.InterfaceError
("bad parameter or other API misuse"). See
scratch/issues/20260601-3-statedb-concurrent-read-flake.md.

Uses a temp-file DB (not :memory:) to mirror real WAL behavior.
"""
from __future__ import annotations

import threading

from rangectl.state import StateDB


def test_concurrent_reads_and_writes_no_interface_error(tmp_path):
    """N threads hammering reads (get_image/list_nodes) while others write
    must not raise sqlite3.InterfaceError on the shared connection."""
    db = StateDB(db_path=str(tmp_path / "state.db"))
    try:
        db.save_topology("t", "deploying", "192.168.100.0/24", "mgmt-br")
        db.add_image("ubuntu", "/img/ubuntu.qcow2")

        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    db.get_image("ubuntu")
                    db.list_nodes("t")
                    db.image_exists("ubuntu")
                    db.get_topology("t")
                    db.list_images()
            except BaseException as exc:
                errors.append(exc)

        def writer(idx: int) -> None:
            try:
                for i in range(150):
                    db.save_node("t", f"n{idx}-{i}", "ubuntu", 1, 1024,
                                 "linux", "running")
            except BaseException as exc:
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(8)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()

        assert not errors, f"concurrent access raised: {errors[0]!r}"
    finally:
        db.close()


def test_concurrent_writers_no_transaction_error(tmp_path):
    """delete_topology/add_image/remove_image must serialize on the shared lock.

    Without the lock these methods issue multi-statement writes on the shared
    connection; two threads interleaving raise sqlite3.OperationalError
    ("cannot start a transaction within a transaction"). Reproduces the crash
    seen deploying two ranges in parallel threads against one StateDB
    (Phase 6, scratch/issues/20260602-1-parallel-test-exploration.md).

    Must use a file-backed DB, not :memory: — :memory: won't reproduce the
    shared-connection transaction interleave.
    """
    db = StateDB(db_path=str(tmp_path / "state.db"))
    try:
        errors: list[BaseException] = []

        def churn(idx: int) -> None:
            try:
                for i in range(60):
                    topo = f"t{idx}-{i}"
                    db.save_topology(topo, "deploying",
                                     "192.168.100.0/24", "mgmt-br")
                    db.add_image(f"img{idx}-{i}", "/img/x.qcow2")
                    db.remove_image(f"img{idx}-{i}")
                    db.delete_topology(topo)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=churn, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writers raised: {errors[0]!r}"
    finally:
        db.close()
