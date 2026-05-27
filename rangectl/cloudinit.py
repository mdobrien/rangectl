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
    return "\n".join(lines) + "\n"


def create_seed_iso(
    output_path: str,
    hostname: str,
    ssh_pubkey: str,
    ifaces: list[dict],
    user: str = "ubuntu",
) -> str:
    """Build a cloud-init seed ISO at output_path.

    ifaces: each dict has keys 'mac', 'ip', 'cidr', and optionally 'gateway'.
    """
    log.info("Creating seed ISO at %s for hostname=%s (ifaces=%d)",
             output_path, hostname, len(ifaces))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
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
