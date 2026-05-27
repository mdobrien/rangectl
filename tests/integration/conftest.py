from __future__ import annotations
import logging
import os
import shutil
from pathlib import Path

import pytest

from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB

# Standard locations on the EC2 host (set up by ec2-bootstrap.sh).
LIBVIRT_IMAGES = Path("/var/lib/libvirt/images")
IMAGE_PATHS = {
    "ubuntu-22.04": LIBVIRT_IMAGES / "jammy-server-cloudimg-amd64.img",
    "ubuntu-24.04": LIBVIRT_IMAGES / "noble-server-cloudimg-amd64.img",
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


@pytest.fixture
def db(tmp_path) -> StateDB:
    state = StateDB(db_path=str(tmp_path / "state.db"))
    for name, path in IMAGE_PATHS.items():
        if path.exists():
            state.add_image(name=name, path=str(path), inject="cloud-init",
                            os_type="linux")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def backend() -> LibvirtBackend:
    return LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)
