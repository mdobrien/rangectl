# Phase 0: EC2 Environment Bootstrap
**Created**: 2026-05-27
**Status**: In Progress
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
- [ ] `ec2-bootstrap.sh` exists in `scratch/scripts/`
- [ ] Script runs successfully on EC2 instance
- [ ] KVM enabled, libvirt running
- [ ] All 3 base images downloaded to `~/.rangectl/images/`
- [ ] Smoke test: VM boots, SSH works, VM destroyed
- [ ] Script is idempotent (safe to re-run)
- [ ] Committed to git

## Gate Output
(paste validation output here when complete)

## Resolution
