"""Topo 7 — mixed VM + container topology.

Verifies that an Ubuntu VM and an nginx Docker container can sit on the same
topology bridge and communicate. Uses ContainerBackend for the container side
and the standard LibvirtBackend for the VM.

Pass criteria:
- VM (client) can ping the container's data-plane IP
- exec() works on both nodes (SSH for VM, `docker exec` for container)
- Container is reachable on TCP/80 (nginx default)
"""
from __future__ import annotations
import logging
import shutil
import time

import pytest

from rangectl import Topology
from rangectl.container_backend import ContainerBackend
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


@pytest.fixture
def container_backend():
    if not shutil.which("docker"):
        pytest.skip("docker not installed on this host")
    return ContainerBackend()


def test_topo7_vm_container_mixed(backend: LibvirtBackend,
                                  container_backend: ContainerBackend,
                                  db: StateDB):
    t = Topology("topo7", backend=backend, db=db,
                 container_backend=container_backend)
    server = t.node("server", container="nginx:latest", vcpu=1, memory=128)
    client = t.node("client", image="ubuntu-22.04", vcpu=1, memory=1024,
                    depends_on=[server])
    # Direct L2 link between container and VM on a /24.
    t.link(server.eth1["10.0.1.1/24"], client.eth1["10.0.1.2/24"])

    with t.deploy() as rng:
        log.info("server mgmt_ip=%s client mgmt_ip=%s",
                 rng["server"].mgmt_ip, rng["client"].mgmt_ip)
        assert rng["server"].mgmt_ip
        assert rng["client"].mgmt_ip

        # Container exec — docker exec path
        r = rng["server"].exec("hostname")
        assert r.exit_code == 0, f"container hostname failed: {r.stderr!r}"
        assert "topo7-server" in r.stdout
        log.info("container hostname: %s", r.stdout.strip())

        # Skip in-container `ip` probe — stock nginx image has no iproute2.
        # Connectivity is proven below via VM-side ping + curl.

        # VM exec — SSH path
        r = rng["client"].exec("hostname")
        assert r.exit_code == 0
        assert "topo7-client" in r.stdout

        # Cross-node ping: VM client → container server. Allow a brief settle
        # for the veth + bridge fabric to stabilize.
        ping = rng["client"].exec("ping -c 3 -W 2 10.0.1.1")
        if ping.exit_code != 0:
            time.sleep(3)
            ping = rng["client"].exec("ping -c 3 -W 2 10.0.1.1")
        assert ping.exit_code == 0, (
            f"VM client -> container server ping failed:\n"
            f"stdout={ping.stdout}\nstderr={ping.stderr}"
        )

        # Sanity that nginx is actually listening so we know docker exec
        # captured a real container process, not just the netns plumbing.
        r = rng["client"].exec("curl -sS -o /dev/null -w '%{http_code}' http://10.0.1.1/")
        # ubuntu cloud images include curl in /usr/bin already.
        assert r.exit_code == 0
        assert r.stdout.strip() == "200", (
            f"curl to nginx returned: stdout={r.stdout!r} stderr={r.stderr!r}"
        )
