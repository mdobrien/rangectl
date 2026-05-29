from __future__ import annotations
import io
import logging
import os
import socket
import stat
import subprocess
import time
from pathlib import Path
from threading import Lock

from rangectl.backend import HostResources
from rangectl.types import ExecResult, VMSpec

log = logging.getLogger(__name__)

KEYS_ROOT = Path("~/.rangectl/keys").expanduser()
SEED_ROOT = Path("~/.rangectl/seeds").expanduser()


def _run(cmd: list[str], check: bool = True, capture: bool = True,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=capture,
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


def _xml_for(spec: VMSpec) -> str:
    """Build libvirt domain XML from VMSpec."""
    if not spec.overlay_path:
        raise ValueError("VMSpec.overlay_path required")

    iface_xml = []
    for ifs in spec.interfaces:
        if not ifs.bridge:
            raise ValueError(
                f"InterfaceSpec for {ifs.node_name}/{ifs.interface_name} "
                "missing bridge — engine must populate it before create_vm"
            )
        if not ifs.mac:
            raise ValueError(
                f"InterfaceSpec for {ifs.node_name}/{ifs.interface_name} "
                "missing mac — engine must populate it before create_vm"
            )
        iface_xml.append(f"""    <interface type='bridge'>
      <source bridge='{ifs.bridge}'/>
      <mac address='{ifs.mac}'/>
      <model type='virtio'/>
    </interface>""")

    cdrom_xml = ""
    if spec.seed_iso_path:
        cdrom_xml = f"""    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{spec.seed_iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
"""

    return f"""<domain type='kvm'>
  <name>{spec.name}</name>
  <memory unit='MiB'>{spec.memory}</memory>
  <currentMemory unit='MiB'>{spec.memory}</currentMemory>
  <vcpu>{spec.vcpu}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{spec.overlay_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
{cdrom_xml}{chr(10).join(iface_xml)}
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
  </devices>
</domain>"""


class LibvirtBackend:
    """Real backend that drives libvirt/QEMU via virsh + paramiko."""

    def __init__(self, ssh_user: str = "ubuntu",
                 ssh_ready_timeout: int = 180) -> None:
        self._lock = Lock()
        self._vm_mgmt_ip: dict[str, str] = {}
        self._vm_topo: dict[str, str] = {}
        self._vm_ssh_user: dict[str, str] = {}
        self._vm_ssh_password: dict[str, str | None] = {}
        self._vyos_bootstrap: dict[str, dict] = {}  # vm_id -> bootstrap payload
        self._topo_keys: dict[str, tuple[str, str]] = {}  # topo_name -> (priv_path, pubkey)
        self._ssh_user = ssh_user
        self._ssh_ready_timeout = ssh_ready_timeout
        KEYS_ROOT.mkdir(parents=True, exist_ok=True)
        SEED_ROOT.mkdir(parents=True, exist_ok=True)

    # --- topology-level helpers ---

    def ssh_pubkey(self, topology_name: str) -> str:
        with self._lock:
            if topology_name in self._topo_keys:
                return self._topo_keys[topology_name][1]
            key_dir = KEYS_ROOT / topology_name
            key_dir.mkdir(parents=True, exist_ok=True)
            priv = key_dir / "id_ed25519"
            pub = key_dir / "id_ed25519.pub"
            if not priv.exists():
                _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv), "-q"])
            os.chmod(priv, 0o600)
            pubkey_text = pub.read_text().strip()
            self._topo_keys[topology_name] = (str(priv), pubkey_text)
            return pubkey_text

    def _priv_key_for(self, topology_name: str) -> str:
        with self._lock:
            if topology_name not in self._topo_keys:
                # Lazily reconstruct from disk if forgotten across processes.
                priv = KEYS_ROOT / topology_name / "id_ed25519"
                pub = KEYS_ROOT / topology_name / "id_ed25519.pub"
                if priv.exists() and pub.exists():
                    self._topo_keys[topology_name] = (str(priv), pub.read_text().strip())
                else:
                    raise RuntimeError(
                        f"no ssh key for topology {topology_name!r}; "
                        "call ssh_pubkey() first"
                    )
            return self._topo_keys[topology_name][0]

    def prepare_vyos_bootstrap(self, vm_id: str, ifaces: list[dict],
                                ssh_pubkey: str, host_ip: str) -> None:
        """Register VyOS first-boot config to be applied via serial console.

        Called by the engine before start(). `ifaces` entries each carry
        `eth_name` (e.g. 'eth0'), `ip`, `cidr`, and an optional `gateway`.
        The mgmt interface should be first.
        """
        with self._lock:
            self._vyos_bootstrap[vm_id] = {
                "ifaces": ifaces,
                "ssh_pubkey": ssh_pubkey,
                "host_ip": host_ip,
            }

    def _bootstrap_vyos_via_console(self, vm_id: str, bootstrap: dict) -> None:
        """Drive `virsh console` with pexpect to apply first-boot VyOS config.

        Assumes the VyOS image is built so interface naming is deterministic
        (PCI-slot order → eth0, eth1, eth2, …). The seed_ifaces eth_name
        field is trusted as-is.
        """
        import pexpect

        ifaces = bootstrap["ifaces"]
        ssh_pubkey = bootstrap["ssh_pubkey"]

        parts = ssh_pubkey.strip().split(None, 2)
        if len(parts) < 2:
            raise ValueError(f"unexpected ssh pubkey format: {ssh_pubkey!r}")
        key_type, key_data = parts[0], parts[1]
        # VyOS accepts the full OpenSSH type string (ssh-ed25519, ssh-rsa, …).
        vyos_type = key_type

        # Pre-bootstrap kernel renames + VyOS hw-id pinning. VyOS's initramfs
        # udev script (vyos_net_name) renames every virtio NIC to e<IFINDEX>
        # — kernel ifindex N starts at 2 (lo=1), so slot-i lands on e<i+2>.
        # VyOS's CLI then rejects those names ("Invalid Ethernet interface
        # name"). We:
        #   1) rename e<i+2> -> eth<i> at the kernel via `ip link set name`
        #      so VyOS sees devices with the names its CLI accepts.
        #   2) `set interfaces ethernet eth<i> hw-id <MAC>` inside configure,
        #      which writes config.boot mappings vyos_net_name reads on
        #      subsequent boots — making the rename persistent.
        rename_cmds: list[tuple[str, str]] = []
        cmds: list[str] = []
        for i, iface in enumerate(ifaces):
            eth = iface.get("eth_name")
            ip = iface.get("ip")
            cidr = iface.get("cidr")
            mac = iface.get("mac")
            if not eth or not ip:
                continue
            kernel_name = f"e{i + 2}"
            if kernel_name != eth:
                rename_cmds.append((kernel_name, eth))
            if mac:
                cmds.append(f"set interfaces ethernet {eth} hw-id {mac}")
            cmds.append(f"set interfaces ethernet {eth} address {ip}/{cidr}")
            gw = iface.get("gateway")
            if gw:
                cmds.append(f"set protocols static route 0.0.0.0/0 next-hop {gw}")
        cmds.append("set service ssh port 22")
        cmds.append("set service ssh listen-address 0.0.0.0")
        cmds.append(
            f"set system login user vyos authentication public-keys "
            f"rangectl type {vyos_type}"
        )
        cmds.append(
            f"set system login user vyos authentication public-keys "
            f"rangectl key {key_data}"
        )

        log.info("[%s] bootstrapping VyOS via serial console (%d cmds)",
                 vm_id, len(cmds))
        child = pexpect.spawn(f"virsh console {vm_id}", encoding="utf-8",
                              timeout=180)
        log_buf: list[str] = []
        child.logfile_read = type("Tee", (), {
            "write": lambda self, s: (log_buf.append(s), None)[1],
            "flush": lambda self: None,
        })()

        prompt_op = r"vyos@vyos:~\$"
        prompt_cfg = r"vyos@vyos#"

        try:
            time.sleep(2)
            child.sendline("")
            child.expect(r"vyos login:", timeout=180)
            child.sendline("vyos")
            child.expect("Password:", timeout=30)
            child.sendline("vyos")
            child.expect(prompt_op, timeout=30)

            child.sendline("stty cols 500")
            child.expect(prompt_op, timeout=10)

            # Kernel-level rename pass: e<i+2> -> eth<i>. Each is a separate
            # sendline so we can see per-command failure in the captured log
            # if a kernel device doesn't exist (e.g. interface count mismatch).
            for src, dst in rename_cmds:
                child.sendline(f"sudo ip link set {src} down")
                child.expect(prompt_op, timeout=15)
                child.sendline(f"sudo ip link set {src} name {dst}")
                child.expect(prompt_op, timeout=15)
                child.sendline(f"sudo ip link set {dst} up")
                child.expect(prompt_op, timeout=15)

            child.sendline("configure")
            child.expect(prompt_cfg, timeout=30)
            for cmd in cmds:
                child.sendline(cmd)
                child.expect(prompt_cfg, timeout=30)

            commit_marker_start = len("".join(log_buf))
            child.sendline("commit")
            child.expect(prompt_cfg, timeout=120)
            commit_output = "".join(log_buf)[commit_marker_start:]
            log.info("[%s] VyOS commit output:\n%s", vm_id, commit_output)
            # VyOS prints CLI errors but does not change the prompt; detect them.
            lower = commit_output.lower()
            if ("error" in lower or "failure" in lower or "invalid" in lower
                    or "not valid" in lower):
                log.error("[%s] VyOS commit reported error. Full console tail:\n%s",
                          vm_id, "".join(log_buf)[-8000:])
                raise RuntimeError(
                    f"VyOS commit failed for {vm_id}; see logs above"
                )

            child.sendline("save")
            child.expect(prompt_cfg, timeout=30)

            # Sanity probe: ask VyOS for its interface state. Captured for logs.
            probe_start = len("".join(log_buf))
            child.sendline("run show interfaces")
            child.expect(prompt_cfg, timeout=30)
            child.sendline("exit")
            child.expect(prompt_op, timeout=15)
            probe_out = "".join(log_buf)[probe_start:]
            log.info("[%s] VyOS interface probe:\n%s", vm_id, probe_out)

            # Operational-mode IP check from the linux side.
            probe2_start = len("".join(log_buf))
            child.sendline("ip -br addr show")
            child.expect(prompt_op, timeout=15)
            probe2_out = "".join(log_buf)[probe2_start:]
            log.info("[%s] VyOS linux addr probe:\n%s", vm_id, probe2_out)
        except Exception:
            log.error("VyOS console bootstrap failed for %s. Console tail:\n%s",
                      vm_id, "".join(log_buf)[-8000:])
            raise
        finally:
            try:
                child.sendcontrol("]")
            except Exception:
                pass
            child.close(force=True)

        log.info("[%s] VyOS bootstrap complete", vm_id)

    # --- VM lifecycle ---

    def create_vm(self, spec: VMSpec) -> str:
        log.info("create_vm: %s (mgmt_ip=%s, ifaces=%d)",
                 spec.name, spec.mgmt_ip, len(spec.interfaces))
        xml = _xml_for(spec)
        log.debug("Domain XML for %s:\n%s", spec.name, xml)
        _run(["virsh", "define", "/dev/stdin"], input_text=xml)
        with self._lock:
            if spec.mgmt_ip:
                self._vm_mgmt_ip[spec.name] = spec.mgmt_ip
            if spec.topology_name:
                self._vm_topo[spec.name] = spec.topology_name
            self._vm_ssh_user[spec.name] = spec.ssh_user
            self._vm_ssh_password[spec.name] = spec.ssh_password
        return spec.name

    def start(self, vm_id: str) -> None:
        log.info("start: %s", vm_id)
        _run(["virsh", "start", vm_id])
        # When a VyOS image is in use cloud-init never runs, so we have to
        # drive the serial console to assign the mgmt IP and install the
        # topology pubkey before SSH on the mgmt subnet will work. The engine
        # supplies the bootstrap config via prepare_vyos_bootstrap() before
        # calling start().
        bootstrap = self._vyos_bootstrap.get(vm_id)
        if bootstrap is not None:
            self._bootstrap_vyos_via_console(vm_id, bootstrap)
        mgmt_ip = self._vm_mgmt_ip.get(vm_id)
        if mgmt_ip:
            self._wait_for_ssh(mgmt_ip)

    def stop(self, vm_id: str) -> None:
        log.info("stop: %s", vm_id)
        res = _run(["virsh", "shutdown", vm_id], check=False)
        if res.returncode != 0:
            log.warning("virsh shutdown failed for %s: %s", vm_id, res.stderr.strip())
        # Allow graceful shutdown briefly; force-destroy on timeout.
        for _ in range(15):
            time.sleep(1)
            state = self._dom_state(vm_id)
            if state in ("shut off", None):
                return
        _run(["virsh", "destroy", vm_id], check=False)

    def destroy(self, vm_id: str) -> None:
        log.info("destroy: %s", vm_id)
        _run(["virsh", "destroy", vm_id], check=False)
        _run(["virsh", "undefine", vm_id, "--remove-all-storage", "--snapshots-metadata", "--nvram"], check=False)
        with self._lock:
            self._vm_mgmt_ip.pop(vm_id, None)
            self._vm_topo.pop(vm_id, None)
            self._vm_ssh_user.pop(vm_id, None)
            self._vm_ssh_password.pop(vm_id, None)
            self._vyos_bootstrap.pop(vm_id, None)

    def _dom_state(self, vm_id: str) -> str | None:
        res = _run(["virsh", "domstate", vm_id], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    # --- snapshots ---

    def snapshot(self, vm_id: str, name: str) -> str:
        log.info("snapshot: %s -> %s", vm_id, name)
        _run(["virsh", "snapshot-create-as", vm_id, name])
        return name

    def restore(self, vm_id: str, snapshot_id: str) -> None:
        log.info("restore: %s @ %s", vm_id, snapshot_id)
        _run(["virsh", "snapshot-revert", vm_id, snapshot_id])
        # snapshot-revert can leave the domain in a non-running state depending
        # on how the snapshot was created (disk-only -> shut off; some kernels
        # -> paused). Force the VM back to running so SSH comes back without
        # the caller having to handle this.
        state = self._dom_state(vm_id) or ""
        if state == "paused":
            _run(["virsh", "resume", vm_id], check=False)
        elif state in ("shut off", "shutoff"):
            _run(["virsh", "start", vm_id], check=False)
        # Wait for SSH to come back before returning so the next exec() doesn't
        # spend its retry budget waiting for the network to settle.
        ip = self._vm_mgmt_ip.get(vm_id)
        if ip:
            self._wait_for_ssh(ip)

    # --- bridges & interfaces ---

    def create_bridge(self, name: str) -> str:
        log.info("create_bridge: %s", name)
        # Idempotent: ignore "exists" errors.
        res = _run(["ip", "link", "add", "name", name, "type", "bridge"], check=False)
        if res.returncode != 0 and "exists" not in (res.stderr or ""):
            raise RuntimeError(f"ip link add failed: {res.stderr}")
        _run(["ip", "link", "set", name, "up"])
        return name

    def delete_bridge(self, name: str) -> None:
        log.info("delete_bridge: %s", name)
        _run(["ip", "link", "set", name, "down"], check=False)
        _run(["ip", "link", "delete", name], check=False)

    def assign_host_ip(self, bridge: str, ip: str, cidr: str) -> None:
        log.info("assign_host_ip: %s -> %s/%s", bridge, ip, cidr)
        res = _run(["ip", "addr", "add", f"{ip}/{cidr}", "dev", bridge], check=False)
        if res.returncode != 0 and "exists" not in (res.stderr or ""):
            raise RuntimeError(f"ip addr add failed: {res.stderr}")

    def attach_interface(self, vm_id: str, bridge: str, mac: str) -> None:
        # Interfaces are inlined into the domain XML at create_vm time. On
        # initial deploy libvirt has already enslaved the TAP to the bridge,
        # so this call is a redundant no-op (re-enslaving the same master
        # is harmless). After Link.down()/up(), however, the bridge was
        # deleted and recreated — the TAP is now orphaned and must be
        # re-enslaved manually for the link to carry traffic again.
        tap = self._find_tap_for_mac(vm_id, mac)
        if not tap:
            log.debug("attach_interface: no TAP found for vm=%s mac=%s",
                      vm_id, mac)
            return
        log.info("attach_interface: enslaving tap=%s to bridge=%s "
                 "(vm=%s mac=%s)", tap, bridge, vm_id, mac)
        _run(["ip", "link", "set", tap, "master", bridge], check=False)
        _run(["ip", "link", "set", tap, "up"], check=False)

    def _find_tap_for_mac(self, vm_id: str, mac: str) -> str | None:
        """Return the host TAP device name for the VM's NIC with the given MAC."""
        res = _run(["virsh", "domiflist", vm_id], check=False)
        if res.returncode != 0:
            return None
        target = mac.lower()
        for line in res.stdout.splitlines():
            fields = line.split()
            # virsh domiflist columns: Interface Type Source Model MAC
            if len(fields) >= 5 and fields[-1].lower() == target:
                tap = fields[0]
                return tap if tap and tap != "-" else None
        return None

    def create_overlay(self, base_image: str, overlay_path: str) -> str:
        log.info("create_overlay: %s -> %s", base_image, overlay_path)
        Path(overlay_path).parent.mkdir(parents=True, exist_ok=True)
        _run([
            "qemu-img", "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", base_image,
            overlay_path,
            "10G",
        ])
        # libvirt-qemu (running as the libvirt-qemu user under the stock
        # AppArmor profile) needs to read the overlay. Group-libvirt is
        # whitelisted on /var/lib/libvirt/images/**.
        try:
            os.chmod(overlay_path, 0o660)
        except OSError:
            pass
        return overlay_path

    # --- SSH / exec ---

    def _wait_for_ssh(self, ip: str) -> None:
        deadline = time.time() + self._ssh_ready_timeout
        last_err = None
        while time.time() < deadline:
            try:
                with socket.create_connection((ip, 22), timeout=3):
                    # Port is open. Try a real handshake too — sshd may be up
                    # before cloud-init has written authorized_keys.
                    pass
                return
            except (OSError, socket.timeout) as exc:
                last_err = exc
                time.sleep(2)
        raise RuntimeError(f"SSH not reachable on {ip} after "
                           f"{self._ssh_ready_timeout}s: {last_err}")

    def _ssh_client(self, vm_id: str):
        import paramiko
        ip = self._vm_mgmt_ip.get(vm_id)
        topo = self._vm_topo.get(vm_id)
        if not ip or not topo:
            raise RuntimeError(
                f"no mgmt_ip or topology recorded for vm {vm_id!r}"
            )
        username = self._vm_ssh_user.get(vm_id, self._ssh_user)
        password = self._vm_ssh_password.get(vm_id)
        # Always attempt the per-topology key first; fall back to password if
        # provided. VyOS images take ~30-90s after SSH port opens before the
        # injected pubkey is committed, so the loop tolerates auth failures.
        key = paramiko.Ed25519Key.from_private_key_file(self._priv_key_for(topo))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        last_err = None
        for _ in range(60):
            try:
                client.connect(
                    ip,
                    username=username,
                    pkey=key,
                    timeout=5,
                    auth_timeout=5,
                    banner_timeout=10,
                    allow_agent=False,
                    look_for_keys=False,
                )
                return client
            except paramiko.AuthenticationException as exc:
                last_err = exc
                if password:
                    try:
                        client.connect(
                            ip,
                            username=username,
                            password=password,
                            timeout=5,
                            auth_timeout=5,
                            banner_timeout=10,
                            allow_agent=False,
                            look_for_keys=False,
                        )
                        return client
                    except Exception as pw_exc:
                        last_err = pw_exc
                time.sleep(2)
            except Exception as exc:
                last_err = exc
                time.sleep(2)
        raise RuntimeError(f"ssh connect to {ip} (user={username}) failed: {last_err}")

    def exec(self, vm_id: str, cmd: str) -> ExecResult:
        log.info("exec %s: %s", vm_id, cmd)
        client = self._ssh_client(vm_id)
        try:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            return ExecResult(exit_code=rc, stdout=out, stderr=err)
        finally:
            client.close()

    def upload(self, vm_id: str, src: str, dst: str) -> None:
        log.info("upload %s: %s -> %s", vm_id, src, dst)
        client = self._ssh_client(vm_id)
        try:
            sftp = client.open_sftp()
            try:
                sftp.put(src, dst)
            finally:
                sftp.close()
        finally:
            client.close()

    # --- resources ---

    def host_resources(self) -> HostResources:
        with open("/proc/cpuinfo") as f:
            cpu_lines = f.read().splitlines()
        total_vcpu = sum(1 for l in cpu_lines if l.startswith("processor"))

        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    try:
                        meminfo[parts[0][:-1]] = int(parts[1])  # KB
                    except ValueError:
                        pass
        total_mem_mb = meminfo.get("MemTotal", 0) // 1024
        avail_mem_mb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) // 1024

        # df on /var/lib/libvirt/images (or /) for available disk
        df = _run(["df", "-Pm", "/var/lib/libvirt/images"], check=False)
        if df.returncode != 0:
            df = _run(["df", "-Pm", "/"])
        rows = df.stdout.strip().splitlines()
        if len(rows) >= 2:
            cols = rows[-1].split()
            total_disk_mb = int(cols[1])
            avail_disk_mb = int(cols[3])
        else:
            total_disk_mb = 0
            avail_disk_mb = 0

        # Reserve some headroom for the host OS itself.
        return HostResources(
            total_vcpu=total_vcpu,
            total_memory_mb=total_mem_mb,
            total_disk_mb=total_disk_mb,
            available_vcpu=max(1, total_vcpu - 1),
            available_memory_mb=max(0, avail_mem_mb - 1024),
            available_disk_mb=avail_disk_mb,
        )
