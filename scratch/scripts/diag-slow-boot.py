#!/usr/bin/env python3
"""Diagnose node-b slow boot (issue 20260529-9).

Deploys a 2-node ns topology (internet=full so node b makes it under a generous
SSH timeout) and EXITS WITHOUT TEARDOWN, leaving the VMs running so we can SSH
into node b (10.255.1.2) and read its boot journal / cloud-init log.

Run on EC2:  sudo PYTHONPATH=/home/ubuntu/rangectl python3 scratch/scripts/diag-slow-boot.py
Then:        sudo ssh -i /root/.rangectl/keys/diagslow/id_ed25519 ubuntu@10.255.1.2 \
                 'systemd-analyze blame | head -20; cloud-init analyze blame | head'
Cleanup:     handled manually after inspection.
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

# Ensure host forwarding is on so internet=full MASQUERADE works.
os.system("sysctl -w net.ipv4.ip_forward=1 >/dev/null")

db = StateDB(db_path="/tmp/diag-slow-state.db")
db.add_image(name="ubuntu-22.04",
             path="/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img",
             inject="cloud-init", os_type="linux")

# Generous SSH timeout so node b's ~215s boot does not fail the deploy.
backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=400)
engine = Engine(backend, db, use_namespaces=True, internet="full")

t = Topology("diagslow")
a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

print(">>> deploying diagslow (NO teardown) ...", flush=True)
rng = engine.deploy(t)
print(">>> DEPLOY DONE. node a=10.255.1.1  node b=10.255.1.2", flush=True)
print(">>> key: /root/.rangectl/keys/diagslow/id_ed25519", flush=True)
print(">>> VMs left running for inspection.", flush=True)
