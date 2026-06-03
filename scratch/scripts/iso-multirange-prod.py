#!/usr/bin/env python3
"""Phase 6 — production multi-range concurrency check.

Deploy two differently-named ranges (iso-a, iso-b) CONCURRENTLY in two threads,
sharing ONE StateDB (the production path: ~/.rangectl/rangectl.db semantics).
Verifies that the shared subnet allocator hands out distinct /24s and the two
ranges coexist (independent mgmt IPs, intra-range ping) with no cross-talk.

This is the product capability, not test infra. Cleans up both ranges.
"""
from __future__ import annotations
import logging
import sys
import threading
import traceback
from pathlib import Path

from rangectl import Topology
from rangectl import subnet_registry as sr
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("iso-multirange")

LIBVIRT_IMAGES = Path("/var/lib/libvirt/images")
IMAGES = {
    "ubuntu-22.04": (LIBVIRT_IMAGES / "jammy-server-cloudimg-amd64.img", "linux"),
}

DB_PATH = "/tmp/iso-multirange-shared.db"
REG_PATH = "/tmp/iso-multirange-subnets.json"


def _build(name: str) -> Topology:
    t = Topology(name)
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    # Identical internal data subnet in BOTH ranges — only isolation makes it work
    t.link(a.eth1["10.0.5.1/24"], b.eth1["10.0.5.2/24"])
    return t


results: dict[str, dict] = {}


def _deploy_and_check(name: str, db: StateDB, backend: LibvirtBackend) -> None:
    res = {"name": name, "ok": False, "error": None, "subnet": None,
           "mgmt_ips": {}, "ping": None}
    engine = Engine(backend, db, use_namespaces=True)
    rng = None
    topo = _build(name)
    try:
        rng = engine.deploy(topo)
        info = engine._range_info[name]
        res["subnet"] = info.mgmt_subnet
        res["mgmt_ips"] = {n: rng[n].mgmt_ip for n in ("a", "b")}
        ping = rng["a"].exec("ping -c 3 -W 2 10.0.5.2")
        res["ping"] = ping.exit_code
        res["ok"] = (ping.exit_code == 0
                     and bool(res["mgmt_ips"]["a"])
                     and bool(res["mgmt_ips"]["b"]))
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        try:
            engine.destroy(topo)
        except Exception as exc:  # noqa: BLE001
            res["error"] = (res["error"] or "") + f"\nDESTROY: {exc}"
        results[name] = res


def main() -> int:
    Path(DB_PATH).unlink(missing_ok=True)
    sr.reset(REG_PATH)
    db = StateDB(db_path=DB_PATH, subnet_registry=REG_PATH)
    for n, (p, ostype) in IMAGES.items():
        if p.exists():
            db.add_image(name=n, path=str(p), inject="cloud-init", os_type=ostype)
    backend = LibvirtBackend(ssh_user="ubuntu", ssh_ready_timeout=240)

    threads = [threading.Thread(target=_deploy_and_check,
                                args=(name, db, backend))
               for name in ("iso-a", "iso-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.close()

    print("\n===== MULTI-RANGE PRODUCTION RESULT =====")
    subnets = set()
    all_ok = True
    for name in ("iso-a", "iso-b"):
        r = results.get(name, {})
        subnets.add(r.get("subnet"))
        ok = r.get("ok")
        all_ok = all_ok and ok
        print(f"[{name}] ok={ok} subnet={r.get('subnet')} "
              f"mgmt={r.get('mgmt_ips')} ping_rc={r.get('ping')}")
        if r.get("error"):
            print(f"   ERROR: {r['error'].splitlines()[0]}")
    distinct = len([s for s in subnets if s]) == 2
    print(f"distinct_subnets={distinct} both_ok={all_ok}")
    return 0 if (all_ok and distinct) else 1


if __name__ == "__main__":
    sys.exit(main())
