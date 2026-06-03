from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.cgroup import Resources
from rangectl.engine import Engine
from rangectl.topology import Link, LiveNode, Range


def _two_node_topo(backend, db, name="rng"):
    t = Topology(name, backend=backend, db=db)
    a = t.node("a", image="ubuntu")
    b = t.node("b", image="ubuntu")
    t.link(a.eth1["10.0.0.1/24"], b.eth1["10.0.0.2/24"])
    return t


# ---------- Range ----------

def test_range_getitem(backend, db):
    t = _two_node_topo(backend, db)
    rng = t.deploy(use_namespaces=False)
    assert isinstance(rng["a"], LiveNode)
    assert rng["a"].name == "a"
    with pytest.raises(KeyError):
        rng["nope"]


def test_range_link_lookup(backend, db):
    t = _two_node_topo(backend, db)
    rng = t.deploy(use_namespaces=False)
    lnk = rng.link("a", "b")
    assert isinstance(lnk, Link)
    # Symmetric lookup
    assert rng.link("b", "a") is lnk
    with pytest.raises(KeyError):
        rng.link("a", "missing")


def test_range_snapshot_records_calls(backend, db):
    t = _two_node_topo(backend, db, "snap")
    rng = t.deploy(use_namespaces=False)
    backend.calls.clear()
    rng.snapshot("checkpoint")
    snap_calls = backend.calls_of("snapshot")
    # one per node
    assert len(snap_calls) == 2
    snap_names = {args[1] for args, _ in snap_calls}
    assert snap_names == {"checkpoint"}


def test_range_restore_uses_db_lookup(backend, db):
    t = _two_node_topo(backend, db, "rst")
    rng = t.deploy(use_namespaces=False)
    rng.snapshot("v1")
    backend.calls.clear()
    rng.restore("v1")
    restore_calls = backend.calls_of("restore")
    assert len(restore_calls) == 2
    # The restore should use the snapshot_id returned by snapshot, not the name.
    for args, _ in restore_calls:
        _, snap_id = args
        assert snap_id.startswith("snap-")


def test_range_logs(backend, db):
    t = _two_node_topo(backend, db, "logs")
    rng = t.deploy(use_namespaces=False)
    all_logs = rng.logs()
    assert isinstance(all_logs, list)
    assert all_logs  # deployment writes events
    assert all(row["topology_name"] == "logs" for row in all_logs)


def test_range_logs_level_filter(backend, db):
    t = _two_node_topo(backend, db, "lvl")
    rng = t.deploy(use_namespaces=False)
    db.log_event("lvl", None, "error", "boom")
    errors = rng.logs(level="error")
    assert len(errors) == 1
    assert errors[0]["message"] == "boom"


# ---------- LiveNode ----------

def test_live_node_exec(backend, db):
    t = _two_node_topo(backend, db, "exec1")
    rng = t.deploy(use_namespaces=False)
    backend.calls.clear()
    rng["a"].exec("uname -a")
    assert backend.calls_of("exec")[0][0][1] == "uname -a"


def test_live_node_upload(backend, db):
    t = _two_node_topo(backend, db, "upl")
    rng = t.deploy(use_namespaces=False)
    backend.calls.clear()
    rng["a"].upload("/local/foo", "/remote/bar")
    upl = backend.calls_of("upload")[0][0]
    assert upl[1] == "/local/foo"
    assert upl[2] == "/remote/bar"


def test_live_node_template_renders_and_uploads(backend, db, tmp_path):
    t = _two_node_topo(backend, db, "tpl")
    rng = t.deploy(use_namespaces=False)
    tmpl_path = tmp_path / "t.j2"
    tmpl_path.write_text("hello {{ name }}, port={{ port }}")
    # The tempfile is cleaned up after upload returns, so capture content
    # inside an upload stub before cleanup happens.
    captured = {}
    original_upload = backend.upload
    def capturing_upload(vm_id, src, dst):
        with open(src) as f:
            captured["content"] = f.read()
        captured["dst"] = dst
        original_upload(vm_id, src, dst)
    backend.upload = capturing_upload
    rng["a"].template(str(tmpl_path), "/etc/cfg", vars={"name": "world", "port": 8080})
    assert captured["dst"] == "/etc/cfg"
    assert "hello world" in captured["content"]
    assert "port=8080" in captured["content"]


def test_live_node_template_cleans_up_tempfile(backend, db, tmp_path):
    """The rendered temp file should not persist after template() returns."""
    t = _two_node_topo(backend, db, "tpl2")
    rng = t.deploy(use_namespaces=False)
    tmpl_path = tmp_path / "t.j2"
    tmpl_path.write_text("x={{ x }}")
    captured = {}
    def fake_upload(vm_id, src, dst):
        captured["src"] = src
    backend.upload = fake_upload  # override after deploy
    rng["a"].template(str(tmpl_path), "/etc/x", vars={"x": 42})
    import os
    assert not os.path.exists(captured["src"])


def test_live_node_logs(backend, db):
    t = _two_node_topo(backend, db, "lnlogs")
    rng = t.deploy(use_namespaces=False)
    db.log_event("lnlogs", "a", "info", "node a event")
    db.log_event("lnlogs", "b", "info", "node b event")
    a_logs = rng["a"].logs()
    msgs = [r["message"] for r in a_logs]
    assert "node a event" in msgs
    assert "node b event" not in msgs


def test_live_node_snapshot_restore(backend, db):
    t = _two_node_topo(backend, db, "lnsnap")
    rng = t.deploy(use_namespaces=False)
    backend.calls.clear()
    snap_id = rng["a"].snapshot("ckpt")
    assert snap_id  # backend assigns one
    rng["a"].restore("ckpt")
    snap_calls = backend.calls_of("snapshot")
    restore_calls = backend.calls_of("restore")
    assert len(snap_calls) == 1
    assert len(restore_calls) == 1
    # Restore should pass through the snapshot_id from DB (not the name).
    _, restored_snap_id = restore_calls[0][0]
    assert restored_snap_id == snap_id


# ---------- Link toggle ----------

def test_link_down(backend, db):
    t = _two_node_topo(backend, db, "down")
    rng = t.deploy(use_namespaces=False)
    lnk = rng.link("a", "b")
    backend.calls.clear()
    lnk.down()
    assert not lnk._is_up
    deletes = backend.calls_of("delete_bridge")
    assert len(deletes) == 1
    # The link's bridge is the one taken down.
    assert deletes[0][0][0] == lnk._bridge_name


def test_link_up(backend, db):
    t = _two_node_topo(backend, db, "up")
    rng = t.deploy(use_namespaces=False)
    lnk = rng.link("a", "b")
    lnk.down()
    backend.calls.clear()
    lnk.up()
    assert lnk._is_up
    creates = backend.calls_of("create_bridge")
    assert len(creates) == 1
    assert creates[0][0][0] == lnk._bridge_name


def test_link_down_without_deploy_raises():
    t = Topology("nowire")
    a = t.node("a", image="ubuntu")
    b = t.node("b", image="ubuntu")
    lnk = t.link(a.eth1["10.0.0.1/24"], b.eth1["10.0.0.2/24"])
    with pytest.raises(RuntimeError):
        lnk.down()


# ---------- Range SDK surface: internet / resources / freeze / thaw -------

def _range(name="sdk", internet="none", resources=None) -> Range:
    return Range(Topology(name), internet=internet, resources=resources)


def test_range_constructor_accepts_internet_and_resources():
    res = Resources(memory="32G", cpus=8)
    rng = _range("lab", internet="full", resources=res)
    assert rng.internet == "full"
    assert rng.resources is res


def test_range_internet_defaults_to_none():
    assert _range().internet == "none"


def test_range_freeze_calls_cgroup(monkeypatch):
    from rangectl import cgroup as cgroup_mod
    calls = []
    monkeypatch.setattr(cgroup_mod, "freeze", lambda n: calls.append(("freeze", n)))
    monkeypatch.setattr(cgroup_mod, "thaw", lambda n: calls.append(("thaw", n)))
    rng = _range("frz")
    rng.freeze()
    rng.thaw()
    assert calls == [("freeze", "frz"), ("thaw", "frz")]


def test_range_enable_internet_calls_module(monkeypatch):
    from rangectl import internet as internet_mod
    calls = []
    monkeypatch.setattr(internet_mod, "enable_internet",
                        lambda n, subnet, veth: calls.append(("enable", n, subnet, veth)))
    monkeypatch.setattr(internet_mod, "disable_internet",
                        lambda n, subnet, veth: calls.append(("disable", n, subnet, veth)))
    rng = _range("inet")
    rng._mgmt_subnet = "10.255.1.0/24"
    rng._veth_host = "mgh1234"
    rng.enable_internet()
    assert rng.internet == "full"
    rng.disable_internet()
    assert rng.internet == "none"
    assert calls == [
        ("enable", "inet", "10.255.1.0/24", "mgh1234"),
        ("disable", "inet", "10.255.1.0/24", "mgh1234"),
    ]


def test_range_enable_internet_requires_namespace_mode():
    rng = _range("legacy")  # no _veth_host wired (legacy deploy)
    with pytest.raises(RuntimeError):
        rng.enable_internet()
    with pytest.raises(RuntimeError):
        rng.disable_internet()
