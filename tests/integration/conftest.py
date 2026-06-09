from __future__ import annotations
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rangectl import subnet_registry
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB

log = logging.getLogger(__name__)

# MASQUERADE source for VM-internet NAT. Ranges are allocated across the whole
# mgmt pool, not just the first /24, so the NAT must cover the pool aggregate.
# Derived from the allocator (honors RANGECTL_MGMT_POOL) so there is exactly
# ONE definition of the pool — the default is 10.255.0.0/16. (Per-range
# internet=full also installs its own MASQUERADE; this blanket rule covers
# ranges that just need outbound for cloud-init regardless of which /24 they
# drew.)
MGMT_SUBNET_CIDR = subnet_registry.pool_aggregate()

# Share ONE host-global subnet registry across every integration process (the
# per-file concurrency harness and pytest-xdist workers each import this
# conftest), so concurrent ranges get distinct /24s instead of all grabbing
# .100.0. A fixed path under /run keeps it host-wide and ephemeral; the run
# harness resets it before each batch. See subnet_registry.py.
os.environ.setdefault("RANGECTL_SUBNET_REGISTRY",
                      "/run/rangectl/mgmt_subnets.json")

# Standard locations on the EC2 host (set up by ec2-bootstrap.sh).
LIBVIRT_IMAGES = Path("/var/lib/libvirt/images")
IMAGE_PATHS = {
    "ubuntu-22.04": (
        LIBVIRT_IMAGES / "jammy-server-cloudimg-amd64.img", "linux"),
    "ubuntu-24.04": (
        LIBVIRT_IMAGES / "noble-server-cloudimg-amd64.img", "linux"),
    "vyos": (
        LIBVIRT_IMAGES / "vyos-rolling-amd64.qcow2", "vyos"),
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


def _have_libvirt() -> bool:
    return shutil.which("virsh") is not None and Path("/dev/kvm").exists()


pytestmark_skip = pytest.mark.skipif(
    not _have_libvirt(),
    reason="Integration tests require libvirt + KVM (run on EC2 c5.metal)",
)


@pytest.fixture(scope="session", autouse=True)
def _stagger_worker_start(request):
    """Spread concurrent xdist workers' first VM boots.

    Without this, ``-n N`` boots every worker's first range at t=0 — a
    thundering herd of QEMU starts that slows each boot enough to blow the
    per-node ssh-ready timeout. Offsetting each worker by 5s smooths the initial
    spike; once workers desync (tests finish at different times) no further
    stagger is needed. No-op outside xdist (worker_id == 'master').
    """
    import re
    import time
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "")
    m = re.match(r"gw(\d+)", worker_id)
    if m:
        delay = int(m.group(1)) * 5
        log.info("worker %s staggering start by %ds", worker_id, delay)
        time.sleep(delay)
    yield


@pytest.fixture
def db(tmp_path) -> StateDB:
    state = StateDB(db_path=str(tmp_path / "state.db"))
    for name, (path, os_type) in IMAGE_PATHS.items():
        if path.exists():
            state.add_image(name=name, path=str(path), inject="cloud-init",
                            os_type=os_type)
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def backend() -> LibvirtBackend:
    return LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)


def _primary_iface() -> str | None:
    """Return the interface name with the default route, or None."""
    r = subprocess.run(
        ["ip", "-o", "-4", "route", "show", "default"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    # Example: "default via 172.31.0.1 dev ens5 proto dhcp src ..."
    parts = r.stdout.split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]
    return None


@pytest.fixture(scope="session", autouse=True)
def vm_internet_nat():
    """Enable IPv4 forwarding + MASQUERADE so VMs on mgmt bridge reach internet.

    Idempotent: skips ops that already exist. Cleans up on session teardown.
    No-op if libvirt is unavailable (local dev) or we can't detect a primary
    interface (no default route — unit-only environment).
    """
    if not _have_libvirt():
        yield
        return
    iface = _primary_iface()
    if not iface:
        log.warning("No default-route interface; skipping VM NAT setup")
        yield
        return

    # Enable forwarding (idempotent; sysctl just sets value).
    subprocess.run(["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
                   check=False, capture_output=True)

    masq_rule = [
        "-t", "nat", "-A", "POSTROUTING",
        "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE",
    ]
    check_rule = ["sudo", "iptables", "-t", "nat", "-C", "POSTROUTING",
                  "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE"]
    existed = subprocess.run(check_rule, capture_output=True).returncode == 0
    if not existed:
        log.info("Adding MASQUERADE: %s -> %s", MGMT_SUBNET_CIDR, iface)
        subprocess.run(["sudo", "iptables", *masq_rule],
                       check=True, capture_output=True)
    else:
        log.info("MASQUERADE already present for %s -> %s",
                 MGMT_SUBNET_CIDR, iface)

    try:
        yield
    finally:
        if not existed:
            log.info("Removing MASQUERADE: %s -> %s", MGMT_SUBNET_CIDR, iface)
            subprocess.run(
                ["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING",
                 "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE"],
                check=False, capture_output=True,
            )
