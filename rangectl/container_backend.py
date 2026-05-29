"""Docker-based backend for container nodes.

A container is a netns. We start it with --network=none and wire each
interface via a veth pair: one end enslaved to the topology bridge on the
host, the other moved into the container's netns, renamed to the desired
guest device, and assigned the configured IP.
"""
from __future__ import annotations
import hashlib
import logging
import subprocess
from threading import Lock

from rangectl.backend import HostResources
from rangectl.types import ExecResult, InterfaceSpec, VMSpec

log = logging.getLogger(__name__)


def _run(cmd: list[str], check: bool = True,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


def _veth_names(vm_id: str, mac: str) -> tuple[str, str]:
    """Short, unique, kernel-legal veth names derived from vm_id+mac.

    Linux caps interface names at 15 chars. We hash the (vm_id, mac) pair to
    keep names compact while still being deterministic per-interface.
    """
    h = hashlib.sha1(f"{vm_id}/{mac}".encode()).hexdigest()[:8]
    return (f"vh{h}", f"vp{h}")


def _guest_dev_name(iface: InterfaceSpec) -> str:
    """Pick the in-container device name for an interface.

    The engine names the management interface "mgmt"; the container places
    it on eth0. Topology link interfaces (declared by the user as eth1,
    eth2, ...) keep their declared name verbatim.
    """
    if iface.interface_name == "mgmt":
        return "eth0"
    return iface.interface_name


class ContainerBackend:

    def __init__(self) -> None:
        self._lock = Lock()
        # vm_id (container name) -> list of InterfaceSpec captured at create_vm.
        # attach_interface() looks up by MAC to find the corresponding spec
        # so it can do the veth + IP wiring with the right guest device name.
        self._specs: dict[str, list[InterfaceSpec]] = {}

    # --- lifecycle -----------------------------------------------------

    def create_vm(self, spec: VMSpec) -> str:
        log.info("create_vm (container): %s image=%s ifaces=%d",
                 spec.name, spec.image, len(spec.interfaces))
        cmd = [
            "docker", "create",
            "--network=none",
            "--cap-add=NET_ADMIN",
            "--cap-add=NET_RAW",
            "--hostname", spec.name,
            "--name", spec.name,
            spec.image,
        ]
        _run(cmd)
        with self._lock:
            self._specs[spec.name] = list(spec.interfaces)
        return spec.name

    def start(self, vm_id: str) -> None:
        log.info("start (container): %s", vm_id)
        _run(["docker", "start", vm_id])

    def stop(self, vm_id: str) -> None:
        log.info("stop (container): %s", vm_id)
        _run(["docker", "stop", vm_id], check=False)

    def destroy(self, vm_id: str) -> None:
        log.info("destroy (container): %s", vm_id)
        _run(["docker", "rm", "-f", vm_id], check=False)
        with self._lock:
            self._specs.pop(vm_id, None)

    # --- exec / file transfer -----------------------------------------

    def exec(self, vm_id: str, cmd: str) -> ExecResult:
        log.info("exec (container) %s: %s", vm_id, cmd)
        # Wrap in sh -c so users can pass shell pipelines and globs.
        res = _run(
            ["docker", "exec", vm_id, "sh", "-c", cmd],
            check=False,
        )
        return ExecResult(
            exit_code=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
        )

    def upload(self, vm_id: str, src: str, dst: str) -> None:
        log.info("upload (container) %s: %s -> %s", vm_id, src, dst)
        _run(["docker", "cp", src, f"{vm_id}:{dst}"])

    # --- bridges (shared semantics with LibvirtBackend) ---------------

    def create_bridge(self, name: str) -> str:
        log.info("create_bridge (container): %s", name)
        res = _run(["ip", "link", "add", "name", name, "type", "bridge"],
                   check=False)
        if res.returncode != 0 and "exists" not in (res.stderr or ""):
            raise RuntimeError(f"ip link add failed: {res.stderr}")
        _run(["ip", "link", "set", name, "up"])
        return name

    def delete_bridge(self, name: str) -> None:
        log.info("delete_bridge (container): %s", name)
        _run(["ip", "link", "set", name, "down"], check=False)
        _run(["ip", "link", "delete", name], check=False)

    def assign_host_ip(self, bridge: str, ip: str, cidr: str) -> None:
        log.info("assign_host_ip (container): %s -> %s/%s", bridge, ip, cidr)
        res = _run(["ip", "addr", "add", f"{ip}/{cidr}", "dev", bridge],
                   check=False)
        if res.returncode != 0 and "exists" not in (res.stderr or ""):
            raise RuntimeError(f"ip addr add failed: {res.stderr}")

    # --- veth wiring ---------------------------------------------------

    def attach_interface(self, vm_id: str, bridge: str, mac: str) -> None:
        spec_ifaces = self._specs.get(vm_id, [])
        target: InterfaceSpec | None = None
        for ifs in spec_ifaces:
            if ifs.mac and ifs.mac.lower() == mac.lower():
                target = ifs
                break
        if target is None:
            log.debug("attach_interface: no iface spec found for vm=%s mac=%s",
                      vm_id, mac)
            return

        host_veth, ctr_veth = _veth_names(vm_id, mac)
        guest_dev = _guest_dev_name(target)

        # If we've already wired this interface (e.g. duplicate attach call
        # on initial deploy + topology link), short-circuit. The host-side
        # veth name is deterministic per (vm_id, mac), so its existence is
        # the marker.
        check = _run(["ip", "link", "show", host_veth], check=False)
        if check.returncode == 0:
            log.debug("attach_interface: veth %s already present, skipping",
                      host_veth)
            return

        # Look up container pid to address its netns.
        inspect = _run(
            ["docker", "inspect", "--format", "{{.State.Pid}}", vm_id],
            check=True,
        )
        pid = inspect.stdout.strip()
        if not pid or pid == "0":
            raise RuntimeError(
                f"container {vm_id!r} has no pid (not running?)"
            )

        log.info("attach_interface (container): vm=%s mac=%s bridge=%s "
                 "host_veth=%s guest_dev=%s pid=%s",
                 vm_id, mac, bridge, host_veth, guest_dev, pid)

        # Create veth pair: host_veth <-> ctr_veth.
        _run(["ip", "link", "add", host_veth, "type", "veth",
              "peer", "name", ctr_veth])
        # Host side: enslave to bridge and bring up.
        _run(["ip", "link", "set", host_veth, "master", bridge])
        _run(["ip", "link", "set", host_veth, "up"])
        # Container side: move into netns, rename, set MAC, assign IP, bring up.
        _run(["ip", "link", "set", ctr_veth, "netns", pid])
        _run(["nsenter", "-t", pid, "-n",
              "ip", "link", "set", ctr_veth, "name", guest_dev])
        _run(["nsenter", "-t", pid, "-n",
              "ip", "link", "set", "dev", guest_dev, "address", mac])
        if target.ip and target.cidr:
            _run(["nsenter", "-t", pid, "-n",
                  "ip", "addr", "add",
                  f"{target.ip}/{target.cidr}", "dev", guest_dev])
        _run(["nsenter", "-t", pid, "-n",
              "ip", "link", "set", guest_dev, "up"])

    # --- noops / not-applicable ---------------------------------------

    def create_overlay(self, base_image: str, overlay_path: str) -> str:
        # Containers have no qcow2 layer; the engine still calls this so we
        # return the unchanged path to keep the interface uniform.
        return overlay_path

    def snapshot(self, vm_id: str, name: str) -> str:
        raise NotImplementedError(
            "Container snapshot deferred to v2 (use docker commit manually)"
        )

    def restore(self, vm_id: str, snapshot_id: str) -> None:
        raise NotImplementedError(
            "Container restore deferred to v2"
        )

    def ssh_pubkey(self, topology_name: str) -> str:
        # Containers exec via `docker exec`, so we don't need a pubkey for
        # them. Returning empty preserves protocol shape; callers using this
        # for containers should not invoke SSH paths.
        return ""

    # --- resources ----------------------------------------------------

    def host_resources(self) -> HostResources:
        # In mixed deployments the libvirt backend already reports host
        # resources. Return a permissive default so engines that only have
        # the container backend can still pass resource validation.
        return HostResources(
            total_vcpu=16,
            total_memory_mb=32768,
            total_disk_mb=500_000,
            available_vcpu=16,
            available_memory_mb=32768,
            available_disk_mb=500_000,
        )
