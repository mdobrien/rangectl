from __future__ import annotations
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _user_data(hostname: str, ssh_pubkey: str, user: str) -> str:
    # Inject the SSH pubkey under both the default cloud-image user and our own
    # so we don't depend on which one the image ships with.
    return f"""#cloud-config
hostname: {hostname}
manage_etc_hosts: true
preserve_hostname: false
users:
  - name: {user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {ssh_pubkey}
ssh_pwauth: false
"""


def _meta_data(hostname: str) -> str:
    return f"instance-id: {hostname}\nlocal-hostname: {hostname}\n"


def _network_config(ifaces: list[dict]) -> str:
    """ifaces: list of {'mac': str, 'ip': str, 'cidr': str, 'gateway': str|None}."""
    lines = ["version: 2", "ethernets:"]
    for i, iface in enumerate(ifaces):
        if not iface.get("ip"):
            continue
        lines.append(f"  if{i}:")
        lines.append("    match:")
        lines.append(f"      macaddress: {iface['mac']}")
        lines.append("    set-name: " + f"if{i}")
        lines.append("    dhcp4: false")
        lines.append("    addresses:")
        lines.append(f"      - {iface['ip']}/{iface['cidr']}")
        if iface.get("gateway"):
            lines.append("    routes:")
            lines.append("      - to: default")
            lines.append(f"        via: {iface['gateway']}")
            lines.append("    nameservers:")
            lines.append("      addresses: [8.8.8.8, 8.8.4.4]")
    return "\n".join(lines) + "\n"


def _vyos_user_data(hostname: str, ssh_pubkey: str, ifaces: list[dict],
                    user: str = "vyos") -> str:
    """Generate VyOS-flavored cloud-config.

    VyOS's cloud-init module honours `vyos_config_commands:` — a list of VyOS
    CLI config commands run in a transaction at first boot. We use it to set
    hostname, configure interfaces, and install the topology SSH pubkey.

    `ifaces` entries must include `eth_name` (e.g. "eth0") in the order they
    appear in the VM's domain XML.
    """
    # Strip OpenSSH-format pubkey: "ssh-ed25519 AAAA... comment"
    parts = ssh_pubkey.strip().split(None, 2)
    if len(parts) < 2:
        raise ValueError(f"unexpected ssh pubkey format: {ssh_pubkey!r}")
    key_type, key_data = parts[0], parts[1]
    # VyOS uses short type names: ssh-ed25519 -> ed25519, ssh-rsa -> ssh-rsa.
    if key_type.startswith("ssh-"):
        vyos_type = key_type[len("ssh-"):] if key_type != "ssh-rsa" else "ssh-rsa"
    else:
        vyos_type = key_type

    cmds: list[str] = [
        f"set system host-name {hostname}",
        "set service ssh port 22",
        "set service ssh listen-address 0.0.0.0",
        f"set system login user {user} authentication public-keys rangectl type {vyos_type}",
        f"set system login user {user} authentication public-keys rangectl key {key_data}",
    ]
    for iface in ifaces:
        eth = iface.get("eth_name")
        ip = iface.get("ip")
        cidr = iface.get("cidr")
        if not eth or not ip:
            continue
        cmds.append(f"set interfaces ethernet {eth} address {ip}/{cidr}")
        gw = iface.get("gateway")
        if gw:
            cmds.append(f"set protocols static route 0.0.0.0/0 next-hop {gw}")

    yaml_cmds = "\n".join(f"  - {c}" for c in cmds)
    return f"""#cloud-config
vyos_config_commands:
{yaml_cmds}
"""


def create_seed_iso(
    output_path: str,
    hostname: str,
    ssh_pubkey: str,
    ifaces: list[dict],
    user: str = "ubuntu",
    flavor: str = "ubuntu",
) -> str:
    """Build a cloud-init seed ISO at output_path.

    ifaces: each dict has keys 'mac', 'ip', 'cidr', and optionally 'gateway'.
    For flavor='vyos', each entry must also have 'eth_name' (e.g. 'eth0').
    """
    log.info("Creating seed ISO at %s for hostname=%s (ifaces=%d, flavor=%s)",
             output_path, hostname, len(ifaces), flavor)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        if flavor == "vyos":
            (tdp / "user-data").write_text(
                _vyos_user_data(hostname, ssh_pubkey, ifaces, user=user)
            )
            (tdp / "meta-data").write_text(_meta_data(hostname))
            # VyOS ignores the netplan-style network-config — interface
            # configuration is done via vyos_config_commands above.
            cmd = [
                "cloud-localds",
                str(out),
                str(tdp / "user-data"),
                str(tdp / "meta-data"),
            ]
        else:
            (tdp / "user-data").write_text(_user_data(hostname, ssh_pubkey, user))
            (tdp / "meta-data").write_text(_meta_data(hostname))
            net_cfg = _network_config(ifaces)
            (tdp / "network-config").write_text(net_cfg)
            cmd = [
                "cloud-localds",
                "-N", str(tdp / "network-config"),
                str(out),
                str(tdp / "user-data"),
                str(tdp / "meta-data"),
            ]
        log.info("cloud-localds: %s", " ".join(cmd))
        if shutil.which("cloud-localds") is None:
            raise RuntimeError("cloud-localds not installed (apt: cloud-image-utils)")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"cloud-localds failed (code {result.returncode}): "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
    return str(out)
