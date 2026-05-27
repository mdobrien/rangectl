# Phase 0: EC2 Environment Bootstrap
**Created**: 2026-05-27
**Status**: Complete
**Phase**: 0

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 0 section
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`

## Goal
Create `scratch/scripts/ec2-bootstrap.sh` that installs all system deps, downloads base images, and runs a smoke test VM on the EC2 instance. No TDD — this is infrastructure bootstrap.

## What the Script Must Do

### 1. System Dependencies (apt)
- `qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`, `virtinst`
- `bridge-utils`, `net-tools`, `cloud-image-utils`
- `python3-pip`, `python3-venv`

### 2. User/Group Setup
- Add `ubuntu` user to `libvirt` and `kvm` groups

### 3. Python Dependencies (pip in venv)
- Create venv at `~/.rangectl/venv`
- Install: `libvirt-python`, `paramiko`, `pytest`, `pyyaml`, `jinja2`

### 4. Download Base Cloud Images
Store in `~/.rangectl/images/`:
- Ubuntu 22.04: `https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img`
- Ubuntu 24.04: `https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img`
- VyOS: rolling nightly qcow2 from `https://github.com/vyos/vyos-rolling-nightly-builds/releases`

### 5. Smoke Test
- Generate a temporary SSH keypair
- Create a cloud-init seed ISO that injects the public key
- Boot Ubuntu 22.04 via `virt-install` with the seed ISO
- Wait for VM to get an IP, SSH in, run `hostname`
- Destroy the VM and clean up

### 6. Validation (script exits non-zero if any fail)
- `kvm-ok` returns success
- `virsh list --all` works
- All three base images exist in `~/.rangectl/images/`
- Smoke test VM booted, SSH'd, destroyed cleanly

## Success Criteria
- [x] `ec2-bootstrap.sh` exists in `scratch/scripts/`
- [x] Script runs successfully on EC2 instance (c5.metal @ 44.192.21.7)
- [x] KVM enabled, libvirt running
- [x] All 3 base images present at `/var/lib/libvirt/images/` (symlinked from `~/.rangectl/images`)
- [x] Smoke test: VM boots, SSH works, VM destroyed
- [x] Script is idempotent (safe to re-run; second run skipped downloads, re-passed smoke)
- [x] Committed to git

## Gate Output
```
==> Installing apt packages...
==> Checking KVM availability...
==>   KVM OK
==> Adding ubuntu to libvirt and kvm groups...
==> Creating /home/ubuntu/.rangectl...
==> Migrating existing /home/ubuntu/.rangectl/images -> /var/lib/libvirt/images
==> Setting up Python venv at /home/ubuntu/.rangectl/venv...
==> Image exists: jammy-server-cloudimg-amd64.img
==> Image exists: noble-server-cloudimg-amd64.img
==> Image exists: vyos-rolling-amd64.iso
==> Smoke test: generating SSH key...
==> Smoke test: building cloud-init seed ISO...
==> Smoke test: creating COW overlay...
==> Smoke test: booting VM with virt-install...
==> Smoke test: waiting for VM IP...
==> Smoke test: VM IP=192.168.122.107
==> Smoke test: waiting for SSH...
==> Smoke test: running hostname over SSH...
==> Smoke test: remote hostname=rangectl-smoke
==> Validating environment...
==>   kvm-ok: OK
==>   virsh: OK
==>   images: OK
==>   smoke test: OK
================================================================
rangectl EC2 bootstrap complete
================================================================
```

## Resolution
The initial failure was an AppArmor block: `~/.rangectl/images/*.img` could not be
opened by qemu. Adding a local override at `/etc/apparmor.d/local/usr.lib.libvirt.qemu`
did not help — that file is not included by the relevant abstraction. The real
gate is `virt-aa-helper`, whose own profile only permits stat'ing files under
`/var/lib/libvirt/images/`, `/var/lib/nova/images/`, and `/var/lib/uvtool/libvirt/images/`.
Disks outside those paths cannot be auto-whitelisted in the per-VM profile.

Fix: store images in `/var/lib/libvirt/images/` (root:libvirt 2775) and surface
them under `~/.rangectl/images` via a symlink. Smoke artifacts (overlay + seed ISO)
are mktemp'd under the same dir for the same reason. Files chowned `user:libvirt`
with 0664/0660 so dynamic ownership flips cleanly.
