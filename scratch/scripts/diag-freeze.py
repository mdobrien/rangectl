"""Diagnostic: confirm whether qemu processes actually land in the range cgroup.

Deploys a 1-VM range with Resources via the ns engine, then inspects where
libvirtd + qemu sit in the cgroup tree relative to rangectl-<name>. Run on EC2:
    sudo python3 scratch/scripts/diag-freeze.py
"""
import subprocess
from pathlib import Path

from rangectl.cgroup import Resources
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB

IMAGES = {"ubuntu-22.04": ("/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img", "linux")}
NAME = "diagfrz"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def main():
    db = StateDB(db_path="/tmp/diag.db")
    for n, (p, t) in IMAGES.items():
        if Path(p).exists():
            db.add_image(name=n, path=p, inject="cloud-init", os_type=t)
    backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)

    from rangectl import Topology
    t = Topology(NAME)
    t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)

    engine = Engine(backend, db, use_namespaces=True, resources=Resources(memory="4G"))
    rng = engine.deploy(t)
    try:
        cg = f"/sys/fs/cgroup/rangectl-{NAME}"
        print("=== cgroup.procs at root of range cgroup ===")
        print(sh(f"cat {cg}/cgroup.procs"))
        print("=== full subtree cgroup.procs ===")
        print(sh(f"find {cg} -name cgroup.procs -exec sh -c 'echo \"-- {{}}\"; cat {{}}' \\;"))
        print("=== qemu host PIDs ===")
        qemu = sh("pgrep -f qemu-system").split()
        print(qemu)
        for pid in qemu:
            print(f"-- /proc/{pid}/cgroup:", sh(f"cat /proc/{pid}/cgroup"))
        info = engine._range_info[NAME]
        print("=== libvirtd wrapper pid (written by engine):", info.pid)
        print("-- /proc/%s/cgroup:" % info.pid, sh(f"cat /proc/{info.pid}/cgroup"))

        a_ip = rng["a"].mgmt_ip
        print("=== ping before freeze ===", sh(f"ping -c2 -W2 {a_ip}; echo rc=$?"))
        rng.freeze()
        print("=== cgroup.events ===", sh(f"cat {cg}/cgroup.events"))
        print("=== ping after freeze ===", sh(f"ping -c2 -W2 {a_ip}; echo rc=$?"))
        rng.thaw()
    finally:
        engine.destroy(t)


if __name__ == "__main__":
    main()
