"""Deploy ONE persistent 2-node range (namespace mode) to the host's default
StateDB, then exit leaving it running — so the rangectl CLI can list/exec/destroy
it. Used to test concurrent multi-range as a PRODUCT capability (drive several
of these at once, then manage them via the CLI).

Usage: sudo python3 iso-cli-deploy.py <range-name>
All ranges use the SAME data subnet (10.0.5.0/24) on purpose — only namespace
isolation makes that non-colliding.
"""
from __future__ import annotations
import sys
import time

from rangectl import StateDB, Topology
from rangectl.libvirt_backend import LibvirtBackend

IMAGE = "ubuntu-22.04"
IMAGE_PATH = "/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img"


def main(name: str) -> int:
    t0 = time.time()
    db = StateDB()  # default ~/.rangectl/rangectl.db (under /root via sudo)
    if not db.image_exists(IMAGE):
        db.add_image(IMAGE, IMAGE_PATH, inject="cloud-init", os_type="linux")
    backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=300)
    t = Topology(name, backend=backend, db=db)
    a = t.node("a", image=IMAGE, vcpu=1, memory=1024)
    b = t.node("b", image=IMAGE, vcpu=1, memory=1024)
    t.link(a.eth1["10.0.5.1/24"], b.eth1["10.0.5.2/24"])
    rng = t.deploy(use_namespaces=True)  # persistent: stays up after exit
    dt = time.time() - t0
    print(f"DEPLOYED {name} in {dt:.0f}s  a={rng['a'].mgmt_ip} b={rng['b'].mgmt_ip}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
