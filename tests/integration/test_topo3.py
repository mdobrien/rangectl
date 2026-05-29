from __future__ import annotations
import logging

import pytest

from rangectl import Topology
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _topo3(backend: LibvirtBackend, db: StateDB) -> Topology:
    """Topo 3: attacker -- vyos-router -- web-server (nginx)."""
    t = Topology("topo3", backend=backend, db=db)
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    attacker = t.node("attacker", image="ubuntu-22.04", vcpu=1, memory=1024,
                      depends_on=[router])
    web = t.node("web", image="ubuntu-22.04", vcpu=1, memory=1024,
                 depends_on=[router])
    web.packages(["nginx"])
    web.service("nginx", enabled=True)

    t.link(attacker.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], web.eth1["10.0.2.2/24"])
    return t


def test_topo3_service_through_router(backend, db):
    topo = _topo3(backend, db)
    with topo.deploy() as rng:
        # Routes for the cross-subnet path.
        rng["attacker"].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
        rng["web"].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")

        # Quick reachability sanity to the web server.
        ping = rng["attacker"].exec("ping -c 3 -W 2 10.0.2.2")
        assert ping.exit_code == 0, (
            f"attacker -> web ping failed:\n{ping.stdout}\n{ping.stderr}"
        )

        # nginx should be up on web (engine ran apt-get install + systemctl
        # start). Give it a moment to be ready.
        web_local = rng["web"].exec(
            "for i in $(seq 1 20); do "
            "curl -fsS -o /dev/null http://127.0.0.1/ && exit 0; "
            "sleep 1; done; exit 1"
        )
        assert web_local.exit_code == 0, (
            f"nginx never came up on web:\n{web_local.stdout}\n{web_local.stderr}"
        )

        # Cross-subnet HTTP: attacker curls web through the router.
        curl = rng["attacker"].exec(
            "curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "
            "http://10.0.2.2/"
        )
        assert curl.exit_code == 0, (
            f"attacker curl through router failed:\n"
            f"stdout={curl.stdout}\nstderr={curl.stderr}"
        )
        assert curl.stdout.strip() == "200", f"unexpected http code: {curl.stdout!r}"
