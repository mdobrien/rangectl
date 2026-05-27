#!/usr/bin/env bash
# rangectl EC2 bootstrap: installs KVM/libvirt, Python deps, base cloud images,
# and runs a smoke-test VM. Idempotent. Must be run as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-ubuntu}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
RANGECTL_DIR="$TARGET_HOME/.rangectl"
VENV_DIR="$RANGECTL_DIR/venv"
# Store images under libvirt's stock path so AppArmor's libvirt-qemu profile
# (which whitelists /var/lib/libvirt/images/** rwk) lets qemu open them. We
# symlink ~/.rangectl/images -> /var/lib/libvirt/images so the SDK can still
# reference the user-friendly path.
IMAGES_DIR="/var/lib/libvirt/images"
IMAGES_LINK="$RANGECTL_DIR/images"

log() { echo "==> $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

#-----------------------------------------------------------------------------
# 1. System dependencies
#-----------------------------------------------------------------------------
log "Installing apt packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    qemu-kvm libvirt-daemon-system libvirt-clients virtinst \
    bridge-utils net-tools cloud-image-utils \
    python3-pip python3-venv \
    pkg-config libvirt-dev \
    cpu-checker curl jq genisoimage sshpass

systemctl enable --now libvirtd
systemctl enable --now virtlogd

# Fail fast if host can't run KVM-accelerated VMs. Without /dev/kvm, virt-install
# falls back to TCG emulation which is too slow for any practical use. On AWS,
# only bare-metal (*.metal) instance types expose nested virtualization.
log "Checking KVM availability..."
if ! kvm-ok >/dev/null 2>&1 || [ ! -e /dev/kvm ]; then
    kvm-ok || true
    echo "" >&2
    echo "FAIL: KVM not available on this host." >&2
    echo "      On AWS, you need a bare-metal instance (e.g. c5.metal, c5n.metal)." >&2
    echo "      Current /proc/cpuinfo vmx|svm flags:" >&2
    grep -Eo '(vmx|svm)' /proc/cpuinfo | sort -u | sed 's/^/        /' >&2
    exit 1
fi
log "  KVM OK"

# /home/<user> defaults to 0750 so libvirt-qemu cannot traverse it to reach
# images. Grant world-execute on the path so qemu can open the disk files.
chmod o+x "$TARGET_HOME"

#-----------------------------------------------------------------------------
# 2. User/group setup
#-----------------------------------------------------------------------------
log "Adding $TARGET_USER to libvirt and kvm groups..."
usermod -aG libvirt "$TARGET_USER"
usermod -aG kvm "$TARGET_USER"

#-----------------------------------------------------------------------------
# 3. Directories
#-----------------------------------------------------------------------------
log "Creating $RANGECTL_DIR..."
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$RANGECTL_DIR"
# Images live in libvirt's stock dir (group-writable by libvirt) and are
# surfaced under ~/.rangectl/images via a symlink for SDK ergonomics.
install -d -o root -g libvirt -m 2775 "$IMAGES_DIR"
if [ -e "$IMAGES_LINK" ] && [ ! -L "$IMAGES_LINK" ]; then
    # First-run migration: a real dir from a prior bootstrap. Move contents
    # into the libvirt path so we don't lose downloads, then replace with link.
    log "Migrating existing $IMAGES_LINK -> $IMAGES_DIR"
    find "$IMAGES_LINK" -mindepth 1 -maxdepth 1 -exec mv -t "$IMAGES_DIR" {} +
    rmdir "$IMAGES_LINK"
fi
ln -sfn "$IMAGES_DIR" "$IMAGES_LINK"
chown -h "$TARGET_USER:$TARGET_USER" "$IMAGES_LINK"
usermod -aG libvirt "$TARGET_USER"  # ensure user can write to images dir

#-----------------------------------------------------------------------------
# 4. Python venv + deps (as target user)
#-----------------------------------------------------------------------------
log "Setting up Python venv at $VENV_DIR..."
sudo -u "$TARGET_USER" bash -s <<EOF
set -euo pipefail
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet libvirt-python paramiko pytest pyyaml jinja2
EOF

#-----------------------------------------------------------------------------
# 5. Download base cloud images
#-----------------------------------------------------------------------------
download_if_missing() {
    local url="$1" dest="$2"
    if [ -s "$dest" ]; then
        log "Image exists: $(basename "$dest")"
        return 0
    fi
    log "Downloading $(basename "$dest")..."
    curl -fL --retry 3 -o "$dest.tmp" "$url"
    mv "$dest.tmp" "$dest"
    # Owned by user but group-libvirt so dynamic ownership flips and reads
    # work cleanly under the stock AppArmor profile.
    chown "$TARGET_USER:libvirt" "$dest"
    chmod 664 "$dest"
}

UBUNTU_22="$IMAGES_DIR/jammy-server-cloudimg-amd64.img"
UBUNTU_24="$IMAGES_DIR/noble-server-cloudimg-amd64.img"

download_if_missing \
    "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img" \
    "$UBUNTU_22"
download_if_missing \
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img" \
    "$UBUNTU_24"

# VyOS — rolling nightly only publishes ISOs (no qcow2 asset). Download the ISO;
# qcow2 build is deferred to Phase 1-2 (when VyOS is needed for Topo 2).
VYOS_ISO="$IMAGES_DIR/vyos-rolling-amd64.iso"

if [ -s "$VYOS_ISO" ]; then
    log "Image exists: $(basename "$VYOS_ISO")"
else
    log "Resolving latest VyOS rolling nightly ISO..."
    VYOS_ISO_URL=$(curl -fsSL \
        https://api.github.com/repos/vyos/vyos-rolling-nightly-builds/releases/latest \
        | jq -r '.assets[] | select(.name | test("\\.iso$")) | .browser_download_url' \
        | head -1)
    [ -n "$VYOS_ISO_URL" ] && [ "$VYOS_ISO_URL" != "null" ] || fail "could not resolve VyOS ISO URL"
    download_if_missing "$VYOS_ISO_URL" "$VYOS_ISO"
fi

#-----------------------------------------------------------------------------
# 6. Smoke test: boot Ubuntu 22.04, SSH in, run hostname, destroy
#-----------------------------------------------------------------------------
SMOKE_NAME="rangectl-smoke-test"
# Keep smoke artifacts under libvirt's images dir so AppArmor allows qemu to
# open the overlay and seed ISO without a custom profile rule.
SMOKE_DIR="$(mktemp -d -p "$IMAGES_DIR" smoke.XXXXXX)"
SMOKE_OVERLAY="$SMOKE_DIR/overlay.qcow2"
SMOKE_SEED="$SMOKE_DIR/seed.iso"
SMOKE_KEY="$SMOKE_DIR/id_ed25519"

cleanup_smoke() {
    log "Cleaning up smoke test..."
    if virsh dominfo "$SMOKE_NAME" >/dev/null 2>&1; then
        virsh destroy "$SMOKE_NAME" >/dev/null 2>&1 || true
        virsh undefine "$SMOKE_NAME" --remove-all-storage >/dev/null 2>&1 || true
    fi
    rm -rf "$SMOKE_DIR"
}
trap cleanup_smoke EXIT

# Wipe any leftover from a previous run before starting
cleanup_smoke
trap cleanup_smoke EXIT
mkdir -p "$SMOKE_DIR"

log "Smoke test: generating SSH key..."
ssh-keygen -t ed25519 -N "" -f "$SMOKE_KEY" -q
SMOKE_PUBKEY=$(cat "$SMOKE_KEY.pub")

log "Smoke test: building cloud-init seed ISO..."
cat > "$SMOKE_DIR/user-data" <<EOF
#cloud-config
hostname: rangectl-smoke
users:
  - name: smoke
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $SMOKE_PUBKEY
ssh_pwauth: false
EOF
cat > "$SMOKE_DIR/meta-data" <<EOF
instance-id: rangectl-smoke
local-hostname: rangectl-smoke
EOF
cloud-localds "$SMOKE_SEED" "$SMOKE_DIR/user-data" "$SMOKE_DIR/meta-data"

log "Smoke test: creating COW overlay..."
qemu-img create -f qcow2 -F qcow2 -b "$UBUNTU_22" "$SMOKE_OVERLAY" 10G >/dev/null

# libvirt-qemu needs read on the disk files; group-libvirt + 0640 is enough.
chmod 755 "$SMOKE_DIR"
chown root:libvirt "$SMOKE_OVERLAY" "$SMOKE_SEED"
chmod 660 "$SMOKE_OVERLAY" "$SMOKE_SEED"

log "Smoke test: booting VM with virt-install..."
virsh net-start default 2>/dev/null || true
virt-install \
    --name "$SMOKE_NAME" \
    --ram 1024 --vcpus 1 \
    --disk "path=$SMOKE_OVERLAY,format=qcow2,bus=virtio" \
    --disk "path=$SMOKE_SEED,device=cdrom" \
    --os-variant ubuntu22.04 \
    --network network=default,model=virtio \
    --graphics none \
    --noautoconsole \
    --import >/dev/null

log "Smoke test: waiting for VM IP..."
VM_IP=""
for i in $(seq 1 60); do
    VM_IP=$(virsh domifaddr "$SMOKE_NAME" --source lease 2>/dev/null \
        | awk '/ipv4/ {split($4,a,"/"); print a[1]; exit}')
    if [ -n "$VM_IP" ]; then
        log "Smoke test: VM IP=$VM_IP"
        break
    fi
    sleep 5
done
[ -n "$VM_IP" ] || fail "smoke VM did not get an IP after 5 min"

log "Smoke test: waiting for SSH..."
SSH_OPTS="-i $SMOKE_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o BatchMode=yes"
SSH_OK=""
for i in $(seq 1 60); do
    if ssh $SSH_OPTS "smoke@$VM_IP" true 2>/dev/null; then
        SSH_OK=1
        break
    fi
    sleep 5
done
[ -n "$SSH_OK" ] || fail "smoke VM SSH did not come up"

log "Smoke test: running hostname over SSH..."
REMOTE_HOSTNAME=$(ssh $SSH_OPTS "smoke@$VM_IP" hostname)
log "Smoke test: remote hostname=$REMOTE_HOSTNAME"
[ "$REMOTE_HOSTNAME" = "rangectl-smoke" ] || fail "hostname mismatch: got '$REMOTE_HOSTNAME'"

SMOKE_PASSED=1
# cleanup happens via trap

#-----------------------------------------------------------------------------
# 7. Validation
#-----------------------------------------------------------------------------
log "Validating environment..."

kvm-ok >/dev/null 2>&1 || fail "kvm-ok failed"
log "  kvm-ok: OK"

virsh list --all >/dev/null 2>&1 || fail "virsh list failed"
log "  virsh: OK"

for f in "$UBUNTU_22" "$UBUNTU_24" "$VYOS_ISO"; do
    [ -s "$f" ] || fail "image missing or empty: $f"
done
log "  images: OK"

[ "${SMOKE_PASSED:-0}" = "1" ] || fail "smoke test did not pass"
log "  smoke test: OK"

cat <<EOF

================================================================
rangectl EC2 bootstrap complete
  images dir : $IMAGES_DIR
  venv       : $VENV_DIR
  user       : $TARGET_USER (in libvirt, kvm groups)
  smoke VM   : booted, SSH'd, destroyed
================================================================
EOF
