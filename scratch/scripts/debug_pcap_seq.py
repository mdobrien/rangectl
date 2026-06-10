"""Phase 21 Gate-2 debug: which echo-request goes missing from a filtered
capture? Deploys a 2-node lab, repeats capture+ping 3x, prints per-packet
tcpdump -r lines (ICMP seq numbers) and the ping summary.

Run on EC2: sudo python3 scratch/scripts/debug_pcap_seq.py
"""
from __future__ import annotations
import subprocess
import sys
import time

sys.path.insert(0, ".")

from rangectl import Range
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import IMAGE_PATHS


class DebugLab(Range):
    name = "pcapdbg"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        self.expect_reach(self.a, "10.0.1.2")


def main():
    db = StateDB(db_path="/tmp/pcapdbg.db")
    for name, (path, os_type) in IMAGE_PATHS.items():
        if path.exists():
            db.add_image(name=name, path=str(path), inject="cloud-init",
                         os_type=os_type)
    backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)
    lab = DebugLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        for round_no in range(1, 4):
            cap = lab.capture("a", "eth1", filter="icmp[icmptype] = 8")
            time.sleep(2)
            t0 = time.time()
            out = lab["a"].run("ping -c 5 10.0.1.2", check=False)
            t1 = time.time()
            print(f"--- round {round_no}: ping took {t1 - t0:.2f}s")
            print(out)
            cap.stop()
            res = subprocess.run(
                ["tcpdump", "-n", "-r", cap.file, "-tt"],
                capture_output=True, text=True)
            print(f"--- round {round_no}: captured "
                  f"{len(res.stdout.splitlines())} packets:")
            print(res.stdout)
            if res.stderr.strip():
                print("stderr:", res.stderr.strip())
    finally:
        lab.destroy()
        db.close()


if __name__ == "__main__":
    main()
