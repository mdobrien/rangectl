#!/usr/bin/env python3
"""Reproduce node-b SLOW boot under internet=none (issue 20260529-9).

Same as diag-slow-boot.py but internet="none" — the deploy relies on the
session/blanket MASQUERADE for boot-time outbound (as the failing test does),
NOT the per-range enable_internet() path. If node b still boots slow here, the
blanket NAT is NOT actually giving the VM outbound in namespace mode, which is
the root cause. Generous SSH timeout so the deploy completes for inspection.

Run on EC2:  sudo PYTHONPATH=/home/ubuntu/rangectl python3 scratch/scripts/diag-slow-boot-none.py
Then SSH into node b (192.168.100.2) with the diagslownone key and read journal.
"""
from __future__ import annotations
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

from rangectl.state import StateDB
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.engine import Engine
from rangectl.topology import Topology

# Mirror the conftest blanket NAT so boot-time outbound is *possible* the same
# way the failing test sets it up: ip_forward on + MASQUERADE for the subnet.
os.system("sysctl -w net.ipv4.ip_forward=1 >/dev/null")
PRIMARY = os.popen("ip -o -4 route show default | awk '{print $5}' | head -1").read().strip()
os.system(f"iptables -t nat -C POSTROUTING -s 192.168.100.0/24 -o {PRIMARY} -j MASQUERADE 2>/dev/null "
          f"|| iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o {PRIMARY} -j MASQUERADE")
print(f">>> blanket NAT: 192.168.100.0/24 -> {PRIMARY}", flush=True)

db = StateDB(db_path="/tmp/diag-slow-none-state.db")
db.add_image(name="ubuntu-22.04",
             path="/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img",
             inject="cloud-init", os_type="linux")

backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=400)
engine = Engine(backend, db, use_namespaces=True, internet="none")

t = Topology("diagslownone")
a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

print(">>> deploying diagslownone (internet=none, NO teardown) ...", flush=True)
rng = engine.deploy(t)
print(">>> DEPLOY DONE. node a=192.168.100.1  node b=192.168.100.2", flush=True)
print(">>> key: /root/.rangectl/keys/diagslownone/id_ed25519", flush=True)
