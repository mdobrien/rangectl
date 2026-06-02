"""Phase 14 integration — rangectl CLI (Gate 2, CLI-specific only).

Deploy a two-node range via the SDK into the DEFAULT state DB + range dir (so a
separate ``python -m rangectl`` process can discover it), then drive the CLI as
a subprocess: list, status, exec, node status, destroy. Proves the CLI operates
on real running ranges cross-process.

Requires libvirt + KVM (EC2), run as root (namespace mode).
"""
from __future__ import annotations
import logging
import subprocess
import sys

import pytest

from rangectl import Range
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import IMAGE_PATHS, pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip

RANGE_NAME = "clilab"


class TwoNodeLab(Range):
    name = RANGE_NAME

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024,
                           depends_on=[self.a])

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        self.expect_reach(self.b, "10.0.1.1")


def _cli(*args: str) -> subprocess.CompletedProcess:
    """Run `python -m rangectl <args>` against the default DB/range dir."""
    cmd = [sys.executable, "-m", "rangectl", *args]
    log.info("CLI: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def default_db_with_images():
    """The default StateDB (what the CLI subprocess reads), pre-loaded with the
    host's ubuntu image so deploy() can resolve it."""
    db = StateDB()  # default ~/.rangectl/rangectl.db
    for name, (path, os_type) in IMAGE_PATHS.items():
        if path.exists():
            db.add_image(name=name, path=str(path), inject="cloud-init",
                         os_type=os_type)
    try:
        yield db
    finally:
        db.close()


def test_cli_full_lifecycle(default_db_with_images):
    db = default_db_with_images
    backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)
    lab = TwoNodeLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        # Drop in-process handles so the CLI reconnects from persisted state.
        del lab

        # list shows the range as running.
        r = _cli("list")
        assert r.returncode == 0, r.stderr
        assert RANGE_NAME in r.stdout
        assert "running" in r.stdout

        # status shows both nodes.
        r = _cli("status", RANGE_NAME)
        assert r.returncode == 0, r.stderr
        assert "a" in r.stdout and "b" in r.stdout

        # status --yaml is machine-readable.
        r = _cli("status", RANGE_NAME, "--yaml")
        assert r.returncode == 0, r.stderr
        assert "nodes:" in r.stdout

        # exec runs on a node and passes through stdout + exit code.
        r = _cli("exec", RANGE_NAME, "a", "--", "hostname")
        assert r.returncode == 0, r.stderr
        assert f"{RANGE_NAME}-a" in r.stdout

        # exec passes through a non-zero remote exit code.
        r = _cli("exec", RANGE_NAME, "a", "--", "false")
        assert r.returncode == 1

        # node status reports the VM power state.
        r = _cli("node", RANGE_NAME, "a", "status")
        assert r.returncode == 0, r.stderr
        assert "running" in r.stdout

        # ssh-config emits a block per node.
        r = _cli("ssh-config", RANGE_NAME)
        assert r.returncode == 0, r.stderr
        assert f"Host {RANGE_NAME}-a" in r.stdout

        # destroy tears it down.
        r = _cli("destroy", RANGE_NAME)
        assert r.returncode == 0, r.stderr

        # list no longer shows it.
        r = _cli("list")
        assert r.returncode == 0, r.stderr
        assert RANGE_NAME not in r.stdout
    finally:
        # Ensure no leak if an assertion failed before destroy().
        try:
            Range.connect(RANGE_NAME).destroy()
        except Exception:
            try:
                Range.cleanup(RANGE_NAME)
            except Exception:
                pass
