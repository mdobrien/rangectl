"""Phase 15: SDK polish — Range lifecycle class, OS drivers, LiveNode sugar."""
from __future__ import annotations

import pytest

from rangectl import (
    ContainerDriver,
    LinuxDriver,
    OSDriver,
    OSType,
    Range,
    VyOSDriver,
    WindowsDriver,
)
from rangectl.topology import LiveNode


# --- helpers ---------------------------------------------------------------

def _exec_cmds(backend):
    return [args[1] for (n, args, _) in backend.calls if n == "exec"]


class _Lab(Range):
    """Minimal two-node lab used across the lifecycle tests."""
    name = "lab"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04")
        self.b = self.node("b", image="ubuntu-22.04", depends_on=[self.a])

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def install_software(self):
        self.b.packages(["nginx"])
        self.b.service("nginx", enabled=True)

    def configure_os(self):
        self.b.route("10.0.2.0/24", via="10.0.1.1")
        self.b.sysctl("net.ipv4.ip_forward", 1)

    def verify(self):
        self.expect_reach(self.b, "10.0.1.1")


# --- Range lifecycle class -------------------------------------------------

def test_range_subclass_deploy(backend, db):
    lab = _Lab()
    lab.deploy(backend=backend, db=db)
    # Nodes became live and addressable.
    assert isinstance(lab["a"], LiveNode)
    assert isinstance(lab["b"], LiveNode)
    # install_software/configure_os ran their commands against the backend.
    cmds = _exec_cmds(backend)
    assert any("apt-get install -y" in c and "nginx" in c for c in cmds)
    assert any("systemctl enable --now nginx" in c for c in cmds)
    assert any("ip route add 10.0.2.0/24 via 10.0.1.1" in c for c in cmds)
    assert any("sysctl -w net.ipv4.ip_forward=1" in c for c in cmds)


def test_verify_required(backend, db):
    class NoVerify(Range):
        name = "noverify"

        def define_nodes(self):
            self.node("a", image="ubuntu-22.04")

    with pytest.raises(RuntimeError, match="verify"):
        NoVerify().deploy(backend=backend, db=db)


def test_name_required():
    class NoName(Range):
        def verify(self):
            pass

    with pytest.raises(RuntimeError, match="name"):
        NoName()


def test_lifecycle_method_order(backend, db):
    order: list[str] = []

    class Ordered(Range):
        name = "ordered"

        def define_nodes(self):
            order.append("define_nodes")
            self.node("a", image="ubuntu-22.04")

        def define_network(self):
            order.append("define_network")

        def install_software(self):
            order.append("install_software")

        def configure_os(self):
            order.append("configure_os")

        def verify(self):
            order.append("verify")

    Ordered().deploy(backend=backend, db=db)
    assert order == [
        "define_nodes", "define_network",
        "install_software", "configure_os", "verify",
    ]


def test_range_repr(backend, db):
    lab = _Lab()
    assert "_Lab" in repr(lab)
    assert "DEFINED" in repr(lab)
    lab.deploy(backend=backend, db=db)
    r = repr(lab)
    assert "RUNNING" in r and "lab" in r


def test_range_context_manager(backend, db):
    with _Lab() as lab:
        lab.deploy(backend=backend, db=db)
        assert lab["a"].name == "a"
    # exiting the block destroyed the range (engine.destroy force-destroys).
    assert any(n == "destroy" for (n, _, _) in backend.calls)


# --- LiveNode enhancements -------------------------------------------------

def _live(backend, os_type=OSType.LINUX):
    backend.vms["vm-1"] = None
    return LiveNode(name="n", mgmt_ip="10.255.1.2", topology_name="t",
                    backend=backend, vm_id="vm-1", os_type=os_type)


def test_livenode_run(backend):
    from rangectl.types import ExecResult
    ln = _live(backend)
    backend.exec_results[("vm-1", "hostname")] = ExecResult(0, "host\n", "")
    assert ln.run("hostname") == "host\n"
    backend.exec_results[("vm-1", "false")] = ExecResult(1, "", "boom")
    with pytest.raises(RuntimeError, match="exit 1"):
        ln.run("false")
    # check=False suppresses the raise.
    assert ln.run("false", check=False) == ""


def test_livenode_put_alias(backend):
    ln = _live(backend)
    ln.put("./a", "/tmp/a")
    uploads = [args for (n, args, _) in backend.calls if n == "upload"]
    assert ("vm-1", "./a", "/tmp/a") in uploads


def test_livenode_route(backend):
    ln = _live(backend)
    ln.route("10.0.2.0/24", via="10.0.1.1")
    assert any("ip route add 10.0.2.0/24 via 10.0.1.1" in c
               for c in _exec_cmds(backend))


def test_livenode_sysctl_packages_service(backend):
    ln = _live(backend)
    ln.sysctl("net.ipv4.ip_forward", 1)
    ln.packages(["curl", "git"])
    ln.service("nginx")
    cmds = _exec_cmds(backend)
    assert any("sysctl -w net.ipv4.ip_forward=1" in c for c in cmds)
    assert any("apt-get install -y curl git" in c for c in cmds)
    assert any("systemctl enable --now nginx" in c for c in cmds)


def test_livenode_repr(backend):
    ln = _live(backend)
    assert "LiveNode" in repr(ln) and "n" in repr(ln)


# --- OS drivers ------------------------------------------------------------

def test_osdriver_base_required_raise():
    d = OSDriver()
    with pytest.raises(NotImplementedError):
        d.exec("x")
    with pytest.raises(NotImplementedError):
        d.put("a", "b")


def test_linux_driver(backend):
    d = LinuxDriver(backend, "vm-1")
    d.add_route("10.0.2.0/24", "10.0.1.1")
    d.set_sysctl("net.ipv4.ip_forward", 1)
    d.install_packages(["nginx", "curl"])
    d.enable_service("nginx")
    cmds = _exec_cmds(backend)
    assert "sudo ip route add 10.0.2.0/24 via 10.0.1.1" in cmds
    assert "sudo sysctl -w net.ipv4.ip_forward=1" in cmds
    assert any("apt-get install -y nginx curl" in c for c in cmds)
    assert "sudo systemctl enable --now nginx" in cmds


def test_vyos_driver(backend):
    d = VyOSDriver(backend, "vm-1")
    d.add_route("10.0.0.0/8", "10.0.1.254")
    cmds = _exec_cmds(backend)
    assert "set protocols static route 10.0.0.0/8 next-hop 10.0.1.254" in cmds


def test_container_driver(backend):
    from rangectl.types import ExecResult
    d = ContainerDriver(backend, "ctr-1")
    backend.exec_results[("ctr-1", "hostname")] = ExecResult(0, "c\n", "")
    assert d.exec("hostname").stdout == "c\n"
    d.put("./f", "/tmp/f")
    assert any(args == ("ctr-1", "./f", "/tmp/f")
               for (n, args, _) in backend.calls if n == "upload")


def test_windows_driver_skeleton():
    d = WindowsDriver()
    for call in (lambda: d.exec("x"), lambda: d.put("a", "b"),
                 lambda: d.add_route("a", "b")):
        with pytest.raises(NotImplementedError):
            call()


def test_ostype_register_custom_driver(backend):
    from rangectl.drivers import make_driver

    class JunosDriver(LinuxDriver):
        def add_route(self, dest, via):
            return self.exec(
                f"set routing-options static route {dest} next-hop {via}")

    OSType.register("junos", JunosDriver)
    d = make_driver("junos", backend, "vm-1")
    assert isinstance(d, JunosDriver)
    d.add_route("0.0.0.0/0", "1.1.1.1")
    assert any("set routing-options static route" in c
               for c in _exec_cmds(backend))
