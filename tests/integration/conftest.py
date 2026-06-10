from __future__ import annotations
import logging
import os
import shutil
from pathlib import Path

import pytest

from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB

log = logging.getLogger(__name__)

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


# Phase 16 (D4): the legacy session-wide `vm_internet_nat` blanket MASQUERADE
# is retired. Internet access is structural now — `ensure_mgmt_ns()` installs
# the host's single static transit MASQUERADE, and only ranges deployed with
# internet="full" (or runtime enable_internet()) get a RANGE-<name> NAT chain
# inside rangectl-mgmt. Tests that install packages must opt into
# internet="full"; "no chain == no internet" is the tested invariant.
