from __future__ import annotations

from rangectl import Topology
from rangectl.dependencies import DependencySet
from rangectl.engine import Engine
from rangectl.topology import LiveNode
from rangectl.types import NodeState


def _topo(name: str = "inj"):
    t = Topology(name)
    t.node("a", image="ubuntu", vcpu=1, memory=1024)
    return t


def _exec_cmds(backend):
    return [args[1] for (n, args, _) in backend.calls if n == "exec"]


def _upload_calls(backend):
    return [args for (n, args, _) in backend.calls if n == "upload"]


def test_inject_packages(backend, db):
    topo = _topo()
    topo._nodes["a"].packages(["nginx", "curl"])
    Engine(backend, db).deploy(topo)

    cmds = _exec_cmds(backend)
    assert any("apt-get install -y" in c and "nginx" in c and "curl" in c for c in cmds)


def test_inject_packages_updates_index_before_install(backend, db):
    """Cloud images ship a stale baked-in apt index — installing from it 404s
    on superseded package versions. apt-get update must precede install."""
    topo = _topo("upd")
    topo._nodes["a"].packages(["nginx"])
    Engine(backend, db).deploy(topo)

    cmds = _exec_cmds(backend)
    update_idx = next(i for i, c in enumerate(cmds) if "apt-get update" in c)
    install_idx = next(i for i, c in enumerate(cmds) if "apt-get install" in c)
    assert update_idx < install_idx


def test_inject_packages_skipped_when_empty(backend, db):
    Engine(backend, db).deploy(_topo("nopkg"))
    # no packages registered, no apt-get exec
    assert not any("apt-get install" in c for c in _exec_cmds(backend))
    assert not any("apt-get update" in c for c in _exec_cmds(backend))


def test_inject_files(backend, db):
    topo = _topo("withfile")
    topo._nodes["a"].file("/etc/myconf", src="local/myconf")
    Engine(backend, db).deploy(topo)

    uploads = _upload_calls(backend)
    # (vm_id, src, dst)
    assert any(u[1] == "local/myconf" and u[2] == "/etc/myconf" for u in uploads)


def test_inject_install(backend, db):
    topo = _topo("install")
    topo._nodes["a"].install(
        name="agent", src="local/agent.tar",
        install_cmd="tar xf /tmp/agent && ./install.sh",
        verify_cmd="agent --version",
    )
    Engine(backend, db).deploy(topo)

    uploads = _upload_calls(backend)
    assert any(u[1] == "local/agent.tar" for u in uploads)
    cmds = _exec_cmds(backend)
    assert any("install.sh" in c for c in cmds)
    assert any("agent --version" in c for c in cmds)


def test_inject_configure(backend, db):
    topo = _topo("conf")
    node = topo._nodes["a"]

    captured: dict = {}

    @node.configure
    def setup(live):
        captured["live"] = live
        live.exec("hello-from-configure")
        live.upload("local/x", "/remote/x")

    Engine(backend, db).deploy(topo)

    assert isinstance(captured.get("live"), LiveNode)
    cmds = _exec_cmds(backend)
    assert "hello-from-configure" in cmds
    uploads = _upload_calls(backend)
    assert any(u[1] == "local/x" and u[2] == "/remote/x" for u in uploads)


def test_inject_services(backend, db):
    topo = _topo("svc")
    topo._nodes["a"].service("nginx", enabled=True)
    Engine(backend, db).deploy(topo)

    cmds = _exec_cmds(backend)
    assert any("systemctl enable nginx" in c for c in cmds)
    assert any("systemctl start nginx" in c for c in cmds)


def test_inject_service_custom_start_cmd(backend, db):
    topo = _topo("svc2")
    topo._nodes["a"].service("custom", enabled=False, start_cmd="/opt/run.sh")
    Engine(backend, db).deploy(topo)

    cmds = _exec_cmds(backend)
    assert "/opt/run.sh" in cmds
    assert not any("systemctl enable custom" in c for c in cmds)


def test_inject_ordering(backend, db):
    topo = _topo("order")
    node = topo._nodes["a"]
    node.packages(["nginx"])
    node.file("/etc/nginx/nginx.conf", src="local/nginx.conf")
    node.install(name="agent", src="local/agent.tar", install_cmd="./install.sh")
    node.service("nginx", enabled=True)

    order_marks: list[str] = []

    @node.configure
    def configure_step(live):
        order_marks.append("configure")
        live.exec("configure-marker")

    Engine(backend, db).deploy(topo)

    # Index of first matching exec/upload call by phase
    call_seq = backend.calls
    idx_pkg = next(i for i, (n, a, _) in enumerate(call_seq)
                   if n == "exec" and "apt-get install" in a[1])
    idx_file = next(i for i, (n, a, _) in enumerate(call_seq)
                    if n == "upload" and a[2] == "/etc/nginx/nginx.conf")
    idx_install = next(i for i, (n, a, _) in enumerate(call_seq)
                       if n == "exec" and "install.sh" in a[1])
    idx_configure = next(i for i, (n, a, _) in enumerate(call_seq)
                         if n == "exec" and a[1] == "configure-marker")
    idx_service = next(i for i, (n, a, _) in enumerate(call_seq)
                       if n == "exec" and "systemctl enable nginx" in a[1])

    assert idx_pkg < idx_file < idx_install < idx_configure < idx_service


def test_inject_with_dependency_set(backend, db):
    ds = DependencySet("web")
    ds.packages(["nginx"])
    ds.service("nginx", enabled=True)

    @ds.configure
    def setup(live):
        live.exec("from-depset")

    topo = _topo("depset")
    topo._nodes["a"].apply(ds)
    Engine(backend, db).deploy(topo)

    cmds = _exec_cmds(backend)
    assert any("apt-get install" in c and "nginx" in c for c in cmds)
    assert "from-depset" in cmds
    assert any("systemctl enable nginx" in c for c in cmds)


def test_inject_transitions_to_running(backend, db):
    topo = _topo("state")
    topo._nodes["a"].packages(["nginx"])
    Engine(backend, db).deploy(topo)
    assert topo._nodes["a"].state == NodeState.RUNNING


def test_live_node_exec_without_backend_raises():
    ln = LiveNode(name="a", mgmt_ip="1.1.1.1", topology_name="t")
    import pytest
    with pytest.raises(RuntimeError):
        ln.exec("x")
    with pytest.raises(RuntimeError):
        ln.upload("a", "b")
