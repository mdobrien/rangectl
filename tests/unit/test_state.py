from __future__ import annotations

import pytest


def test_allocate_mgmt_subnet_first(db):
    subnet = db.allocate_mgmt_subnet("topo-a")
    assert subnet == "192.168.100.0/24"


def test_allocate_mgmt_subnet_sequential(db):
    a = db.allocate_mgmt_subnet("topo-a")
    b = db.allocate_mgmt_subnet("topo-b")
    c = db.allocate_mgmt_subnet("topo-c")
    assert a == "192.168.100.0/24"
    assert b == "192.168.101.0/24"
    assert c == "192.168.102.0/24"


def test_free_mgmt_subnet_releases_for_reuse(db):
    a = db.allocate_mgmt_subnet("topo-a")
    b = db.allocate_mgmt_subnet("topo-b")
    db.free_mgmt_subnet("topo-a")
    c = db.allocate_mgmt_subnet("topo-c")
    assert c == a  # reuses freed /24


def test_save_and_get_topology(db):
    db.save_topology("t1", "active", "192.168.100.0/24", "rlmgt-deadbe")
    row = db.get_topology("t1")
    assert row is not None
    assert row["name"] == "t1"
    assert row["status"] == "active"
    assert row["mgmt_subnet"] == "192.168.100.0/24"
    assert row["mgmt_bridge"] == "rlmgt-deadbe"


def test_get_topology_missing(db):
    assert db.get_topology("nope") is None


def test_list_topologies(db):
    db.save_topology("t1", "active", "192.168.100.0/24", "br1")
    db.save_topology("t2", "active", "192.168.101.0/24", "br2")
    names = sorted(t["name"] for t in db.list_topologies())
    assert names == ["t1", "t2"]


def test_delete_topology(db):
    db.save_topology("t1", "active", "192.168.100.0/24", "br1")
    db.delete_topology("t1")
    assert db.get_topology("t1") is None
    assert db.list_topologies() == []


def test_save_and_update_node(db):
    db.save_topology("t1", "active", "192.168.100.0/24", "br1")
    db.save_node("t1", "n1", image="ubuntu", vcpu=2, memory_mb=2048,
                 os_type="linux", state="defined", mgmt_ip="192.168.100.1")
    db.update_node_state("t1", "n1", "ready")
    cur = db._conn.execute(
        "SELECT state, mgmt_ip FROM nodes WHERE topology_name=? AND name=?",
        ("t1", "n1"),
    )
    state, mgmt_ip = cur.fetchone()
    assert state == "ready"
    assert mgmt_ip == "192.168.100.1"


def test_log_event_and_get_logs(db):
    db.log_event("t1", "n1", "INFO", "starting")
    db.log_event("t1", "n1", "ERROR", "boom")
    db.log_event("t1", None, "INFO", "topo event")
    db.log_event("t2", "x", "INFO", "other")

    all_t1 = db.get_logs("t1")
    assert len(all_t1) == 3

    n1_only = db.get_logs("t1", node_name="n1")
    assert len(n1_only) == 2
    assert {r["message"] for r in n1_only} == {"starting", "boom"}

    errors = db.get_logs("t1", level="ERROR")
    assert len(errors) == 1
    assert errors[0]["message"] == "boom"


def test_image_crud(db):
    assert db.image_exists("u22") is False
    db.add_image("u22", "/img/u22.qcow2", inject="cloud-init",
                 os_type="linux", size_mb=2048, built_from=None)
    assert db.image_exists("u22") is True

    got = db.get_image("u22")
    assert got["name"] == "u22"
    assert got["path"] == "/img/u22.qcow2"
    assert got["inject"] == "cloud-init"
    assert got["os_type"] == "linux"
    assert got["size_mb"] == 2048
    assert got["built_from"] is None

    db.add_image("u22-nginx", "/img/u22-nginx.qcow2", built_from="u22")
    names = sorted(i["name"] for i in db.list_images())
    assert names == ["u22", "u22-nginx"]

    db.remove_image("u22-nginx")
    assert db.image_exists("u22-nginx") is False
    assert db.get_image("u22-nginx") is None
