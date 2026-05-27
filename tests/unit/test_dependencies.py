from __future__ import annotations

from rangectl import Topology
from rangectl.dependencies import DependencySet
from rangectl.types import OSType


def _node():
    t = Topology("t")
    return t.node("n", image="ubuntu")


def test_dependency_set_packages():
    ds = DependencySet("web")
    ds.packages(["nginx", "curl"])
    assert ds._packages == ["nginx", "curl"]


def test_dependency_set_install():
    ds = DependencySet("web")
    ds.install(name="agent", src="agent.tar", install_cmd="./install.sh",
               verify_cmd="agent --version")
    assert len(ds._installs) == 1
    inst = ds._installs[0]
    assert inst.name == "agent"
    assert inst.src == "agent.tar"
    assert inst.install_cmd == "./install.sh"
    assert inst.verify_cmd == "agent --version"


def test_dependency_set_configure():
    ds = DependencySet("web")

    @ds.configure
    def setup(node):
        node.exec("echo hi")

    assert len(ds._configure_fns) == 1
    assert ds._configure_fns[0] is setup


def test_dependency_set_service():
    ds = DependencySet("web")
    ds.service("nginx", enabled=True, start_cmd="systemctl start nginx")
    assert len(ds._services) == 1
    svc = ds._services[0]
    assert svc.name == "nginx"
    assert svc.enabled is True
    assert svc.start_cmd == "systemctl start nginx"


def test_dependency_set_file():
    ds = DependencySet("web")
    ds.file("/etc/nginx/nginx.conf", src="local/nginx.conf")
    assert ds._files == [("/etc/nginx/nginx.conf", "local/nginx.conf")]


def test_dependency_set_apply_merges():
    ds = DependencySet("web")
    ds.packages(["nginx"])
    ds.install(name="agent", src="a.tar", install_cmd="./i.sh")
    ds.service("nginx", enabled=True)
    ds.file("/etc/c", src="local/c")

    @ds.configure
    def fn(n):
        pass

    node = _node()
    node.apply(ds)

    assert node._packages == ["nginx"]
    assert len(node._installs) == 1 and node._installs[0].name == "agent"
    assert len(node._services) == 1
    assert node._files == [("/etc/c", "local/c")]
    assert len(node._configure_fns) == 1


def test_apply_ordering_preserved():
    ds1 = DependencySet("a")
    ds1.packages(["pkg-a1", "pkg-a2"])
    ds2 = DependencySet("b")
    ds2.packages(["pkg-b1"])

    node = _node()
    node.packages(["pkg-x"])
    node.apply(ds1)
    node.apply(ds2)

    assert node._packages == ["pkg-x", "pkg-a1", "pkg-a2", "pkg-b1"]
