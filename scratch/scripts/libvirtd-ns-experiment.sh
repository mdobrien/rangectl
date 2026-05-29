#!/bin/bash
# Experiment: can libvirtd run inside a per-range PID+net+mount namespace?
#
# Phases:
#   A  start libvirtd inside ns, talk to it from host
#   B  define & boot a VM through that libvirtd, verify qemu PID lineage
#   C  kill the supervisor, verify qemu was reaped
#   D  exercise virsh console (PTY round-trip)
#   E  run two ranges concurrently to check AppArmor profile collisions
#
# All output is verbose so the failure mode is obvious.

set -u
RID_A=ns-a
RID_B=ns-b
BASE_IMG=/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img

cleanup() {
    echo "=== cleanup ==="
    for r in $RID_A $RID_B; do
        if [[ -f /tmp/$r/supervisor.pid ]]; then
            pid=$(cat /tmp/$r/supervisor.pid 2>/dev/null || true)
            [[ -n "$pid" ]] && sudo kill -9 "$pid" 2>/dev/null || true
        fi
        sudo rm -rf /tmp/$r 2>/dev/null || true
    done
    # any stray libvirtd children
    sudo pkill -9 -f 'libvirtd.*tmp/ns-' 2>/dev/null || true
    sudo pkill -9 -f 'qemu-system.*ns-' 2>/dev/null || true
}
trap cleanup EXIT

make_range() {
    local rid=$1
    sudo rm -rf /tmp/$rid
    # Mirror the host's libvirt layout so we can bind-mount over it in-ns.
    sudo mkdir -p /tmp/$rid/{run-libvirt,lib-libvirt,log-libvirt,cache-libvirt,etc-libvirt,images}
    sudo mkdir -p /tmp/$rid/run-libvirt/{network,qemu,storage,interface,nodedev,nwfilter,secrets,common}
    sudo mkdir -p /tmp/$rid/lib-libvirt/{qemu,images,boot,filesystems,swtpm,dnsmasq}
    sudo chmod -R 0777 /tmp/$rid

    # qemu.conf — disable security driver + dynamic ownership; we share /dev/kvm.
    cat <<EOF | sudo tee /tmp/$rid/etc-libvirt/qemu.conf >/dev/null
security_driver = "none"
user = "root"
group = "root"
dynamic_ownership = 0
remember_owner = 0
stdio_handler = "file"
EOF

    # libvirtd.conf — keep host default socket path (/run/libvirt/libvirt-sock)
    # because that path will be bind-mounted onto /tmp/$rid/run-libvirt below.
    cat <<EOF | sudo tee /tmp/$rid/libvirtd.conf >/dev/null
log_level = 3
log_outputs = "1:file:/var/log/libvirt/libvirtd.log"
auth_unix_rw = "none"
unix_sock_rw_perms = "0777"
EOF
}

start_supervisor() {
    # Launch a supervisor (PID 1 in new ns) that exec's libvirtd.
    # We need: new PID ns, new net ns, new mount ns, /proc remounted.
    # libvirtd reads /etc/libvirt/qemu.conf by default — override with LIBVIRT_*_DIR
    local rid=$1
    local logf=/tmp/$rid/supervisor.out

    # `unshare -fp` forks so the child becomes PID 1 inside.
    # We background and capture host PID of the unshare invocation.
    sudo nohup unshare --pid --fork --net --mount --uts \
        --propagation private \
        --mount-proc \
        bash -c "
            set -e
            # Redirect only the libvirt state dirs that must be per-range.
            # /var/lib/libvirt/images stays shared with the host (image registry).
            mount --bind /tmp/$rid/run-libvirt /run/libvirt
            for sub in qemu dnsmasq boot swtpm; do
                [[ -d /var/lib/libvirt/\$sub ]] || continue
                mount --bind /tmp/$rid/lib-libvirt/\$sub /var/lib/libvirt/\$sub
            done
            mount --bind /tmp/$rid/log-libvirt   /var/log/libvirt
            mount --bind /tmp/$rid/cache-libvirt /var/cache/libvirt
            mount --bind /tmp/$rid/etc-libvirt   /etc/libvirt
            # Block dbus so libvirt doesn't call systemd-machined across the
            # PID-ns boundary (host receives our local PIDs, mis-resolves them).
            mkdir -p /tmp/$rid/empty
            [[ -d /run/dbus ]] && mount --bind /tmp/$rid/empty /run/dbus
            ip link set lo up
            echo \$\$ > /tmp/$rid/supervisor.pid.ns
            exec /usr/sbin/libvirtd \
                --config /tmp/$rid/libvirtd.conf \
                --pid-file /run/libvirt/libvirtd.pid
        " > "$logf" 2>&1 &

    # The PID returned is the *host* PID of the supervisor (the unshare process).
    sleep 0.2
    # Find the actual supervisor PID — the unshare child.
    local sup_pid=$(sudo pgrep -f "unshare.*pid.*$rid" | head -1)
    echo "$sup_pid" | sudo tee /tmp/$rid/supervisor.pid >/dev/null
    echo "[$rid] supervisor host-PID=$sup_pid"
}

wait_socket() {
    local rid=$1
    # Socket lives at /run/libvirt/libvirt-sock *inside* the namespace; bind
    # source on the host is /tmp/$rid/run-libvirt/libvirt-sock.
    for i in $(seq 1 30); do
        if [[ -S /tmp/$rid/run-libvirt/libvirt-sock ]]; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

phaseA() {
    echo
    echo "================ PHASE A: libvirtd starts in ns =================="
    make_range $RID_A
    start_supervisor $RID_A
    if ! wait_socket $RID_A; then
        echo "FAIL: libvirt socket never appeared at /tmp/$RID_A/run/libvirt-sock"
        echo "--- supervisor.out ---"
        sudo cat /tmp/$RID_A/supervisor.out
        echo "--- libvirtd.log ---"
        sudo cat /tmp/$RID_A/log/libvirtd.log 2>/dev/null || echo "(no log)"
        return 1
    fi
    echo "OK: socket present"
    echo "--- virsh from host ---"
    sudo virsh -c "qemu+unix:///system?socket=/tmp/$RID_A/run-libvirt/libvirt-sock" version
    sudo virsh -c "qemu+unix:///system?socket=/tmp/$RID_A/run-libvirt/libvirt-sock" list --all
    echo "--- process tree of supervisor ---"
    sudo pstree -p $(cat /tmp/$RID_A/supervisor.pid)
    echo "PHASE A: PASS"
}

phaseB() {
    echo
    echo "================ PHASE B: boot a VM via per-ns libvirtd =========="
    # Create a thin overlay
    sudo qemu-img create -f qcow2 -F qcow2 -b $BASE_IMG /tmp/$RID_A/images/vm1.qcow2 4G

    # Minimal domain XML — no network, just a disk + serial
    cat <<EOF | sudo tee /tmp/$RID_A/vm1.xml >/dev/null
<domain type='kvm'>
  <name>vm1</name>
  <memory unit='MiB'>512</memory>
  <vcpu>1</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/tmp/$RID_A/images/vm1.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
EOF
    local URI="qemu+unix:///system?socket=/tmp/$RID_A/run-libvirt/libvirt-sock"
    echo "--- define ---"
    sudo virsh -c "$URI" define /tmp/$RID_A/vm1.xml
    echo "--- start ---"
    sudo virsh -c "$URI" start vm1 || { echo "FAIL: virsh start"; sudo tail -40 /tmp/$RID_A/log/libvirtd.log; return 1; }

    # Begin console capture immediately so we don't miss early boot output.
    local pty=$(sudo virsh -c "$URI" ttyconsole vm1)
    echo "console PTY for vm1: $pty"
    sudo nohup bash -c "cat '$pty' > /tmp/$RID_A/console.cap 2>&1" >/dev/null 2>&1 &
    sleep 2
    echo "--- list ---"
    sudo virsh -c "$URI" list
    echo "--- qemu process tree (host view) ---"
    sudo pstree -p $(cat /tmp/$RID_A/supervisor.pid)
    echo "--- qemu PID is whose child? ---"
    qpid=$(sudo pgrep -f 'qemu-system.*vm1' | head -1)
    if [[ -n "$qpid" ]]; then
        echo "qemu host PID=$qpid  ppid=$(ps -o ppid= -p $qpid)"
        echo "libvirtd in ns PID=$(cat /tmp/$RID_A/libvirtd.pid 2>/dev/null || echo unknown)"
        # The supervisor's host PID:
        echo "supervisor host PID=$(cat /tmp/$RID_A/supervisor.pid)"
    else
        echo "FAIL: no qemu process found"
        return 1
    fi
    echo "PHASE B: PASS"
}

phaseC() {
    echo
    echo "================ PHASE C: kill ns PID-1 -> clean reap ============"
    echo "--- ps aux | grep ns-a (full host view) ---"
    sudo ps auxf | grep -E 'unshare|libvirtd|qemu-system.*guest|ns-a' | grep -v grep || echo "(none)"
    echo
    # Find libvirtd's host PID via /proc — match arg0 exactly, then RID in argv.
    local libv_host=""
    for pid in $(ls /proc/ | grep -E '^[0-9]+$' | sort -n); do
        local args=$(sudo tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
        if [[ "$args" == "/usr/sbin/libvirtd "*"$RID_A"* ]]; then
            libv_host=$pid
            break
        fi
    done
    local qemu_host=""
    for pid in $(ls /proc/ | grep -E '^[0-9]+$' | sort -n); do
        local args=$(sudo tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
        if [[ "$args" == "/usr/bin/qemu-system-x86_64 "*"guest=vm1"* ]]; then
            qemu_host=$pid
            break
        fi
    done
    echo "libvirtd host-PID=$libv_host  qemu host-PID=$qemu_host"
    if [[ -z "$libv_host" || -z "$qemu_host" ]]; then
        echo "FAIL: could not locate libvirtd or qemu on host"
        return 1
    fi
    echo "--- /proc/\$qemu/status (NSpid = host vs ns view) ---"
    sudo grep -E '^(Name|Pid|PPid|NSpid|NStgid):' /proc/$qemu_host/status
    echo "--- qemu host ppid ---"
    ps -o pid,ppid,comm -p $qemu_host
    echo "--- libvirtd host ppid ---"
    ps -o pid,ppid,comm -p $libv_host
    echo "--- pstree of libvirtd ---"
    sudo pstree -p $libv_host
    echo
    echo "--- killing libvirtd host-PID=$libv_host ---"
    sudo kill -9 $libv_host
    sleep 3
    echo "--- after kill: any guest=vm1 qemu still alive? ---"
    local survivor=""
    for pid in $(ls /proc/ | grep -E '^[0-9]+$'); do
        if sudo grep -aq 'guest=vm1' /proc/$pid/cmdline 2>/dev/null; then
            survivor=$pid
            break
        fi
    done
    if [[ -n "$survivor" ]]; then
        echo "FAIL: qemu (PID=$survivor) survived libvirtd kill"
        echo "qemu's new ppid: $(ps -o ppid= -p $survivor)"
        return 1
    fi
    echo "PHASE C: PASS — qemu reaped after libvirtd kill"
}

find_libv() {
    local rid=$1
    for pid in $(ls /proc/ | grep -E '^[0-9]+$' | sort -n); do
        local args=$(sudo tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
        if [[ "$args" == "/usr/sbin/libvirtd "*"$rid"* ]]; then
            echo $pid
            return 0
        fi
    done
}

find_qemu() {
    local name=$1
    for pid in $(ls /proc/ | grep -E '^[0-9]+$' | sort -n); do
        local args=$(sudo tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
        if [[ "$args" == "/usr/bin/qemu-system-x86_64 "*"guest=$name"* ]]; then
            echo $pid
            return 0
        fi
    done
}

phaseD() {
    echo
    echo "================ PHASE D: virsh console PTY round-trip =========="
    echo "--- waiting 30s for VM to emit kernel boot output ---"
    sleep 30
    local bytes=$(sudo wc -c < /tmp/$RID_A/console.cap 2>/dev/null || echo 0)
    echo "captured $bytes bytes from PTY"
    echo "--- first 1500 chars of capture ---"
    sudo head -c 1500 /tmp/$RID_A/console.cap
    echo
    echo "--- end of capture ---"
    if [[ "$bytes" -lt 100 ]]; then
        echo "FAIL: console produced <100 bytes"
        return 1
    fi
    # Sanity: did we capture actual Linux boot output?
    if ! sudo grep -aqE 'Linux|kernel|systemd|Booting|initrd' /tmp/$RID_A/console.cap; then
        echo "FAIL: capture has no boot-like markers"
        return 1
    fi
    echo "PHASE D: PASS — $bytes bytes of authentic guest serial output"
}

phaseE() {
    echo
    echo "================ PHASE E: two ranges concurrently ================"
    # Range A is still up from Phase A-D. Start range B alongside.
    make_range $RID_B
    start_supervisor $RID_B
    wait_socket $RID_B || { echo "FAIL: range B socket"; return 1; }

    local URI_A="qemu+unix:///system?socket=/tmp/$RID_A/run-libvirt/libvirt-sock"
    local URI_B="qemu+unix:///system?socket=/tmp/$RID_B/run-libvirt/libvirt-sock"

    # Boot a VM in B with a different name to make sure the two libvirtds
    # don't share any state.
    sudo qemu-img create -f qcow2 -F qcow2 -b $BASE_IMG /tmp/$RID_B/images/vm2.qcow2 4G
    sudo sed 's/vm1/vm2/g; s|/tmp/ns-a/|/tmp/ns-b/|g' /tmp/$RID_A/vm1.xml | sudo tee /tmp/$RID_B/vm2.xml >/dev/null
    sudo virsh -c "$URI_B" define /tmp/$RID_B/vm2.xml
    sudo virsh -c "$URI_B" start vm2 || { echo "FAIL: range B vm2 start"; return 1; }
    sleep 2

    echo "--- range A virsh list (should show vm1 only) ---"
    sudo virsh -c "$URI_A" list
    echo "--- range B virsh list (should show vm2 only) ---"
    sudo virsh -c "$URI_B" list

    # Cross-check: ranges should not see each other's VMs.
    if sudo virsh -c "$URI_A" list --all | grep -q vm2; then
        echo "FAIL: range A sees vm2 (cross-namespace leak)"; return 1
    fi
    if sudo virsh -c "$URI_B" list --all | grep -q vm1; then
        echo "FAIL: range B sees vm1 (cross-namespace leak)"; return 1
    fi

    # Verify both qemu processes are alive on the host, in different ns groups.
    local q1=$(find_qemu vm1)
    local q2=$(find_qemu vm2)
    echo "vm1 host-PID=$q1   vm2 host-PID=$q2"
    if [[ -z "$q1" || -z "$q2" ]]; then
        echo "FAIL: one of the qemu processes is missing"; return 1
    fi

    echo "--- NSpid for each qemu (col1=host, col2+=ns) ---"
    echo -n "vm1: "; sudo grep '^NSpid:' /proc/$q1/status
    echo -n "vm2: "; sudo grep '^NSpid:' /proc/$q2/status

    echo "--- PID namespace inodes (should differ) ---"
    local ns1=$(sudo readlink /proc/$q1/ns/pid)
    local ns2=$(sudo readlink /proc/$q2/ns/pid)
    echo "vm1 pidns: $ns1"
    echo "vm2 pidns: $ns2"
    if [[ "$ns1" == "$ns2" ]]; then
        echo "FAIL: VMs share a PID namespace"; return 1
    fi

    # Kill range B's libvirtd, verify only vm2 dies, vm1 survives.
    local libvB=$(find_libv $RID_B)
    echo "--- killing range B libvirtd host-PID=$libvB ---"
    sudo kill -9 $libvB
    sleep 3

    if [[ -n "$(find_qemu vm2)" ]]; then
        echo "FAIL: vm2 survived range B teardown"; return 1
    fi
    if [[ -z "$(find_qemu vm1)" ]]; then
        echo "FAIL: vm1 died when range B was torn down (isolation broken)"; return 1
    fi
    echo "PHASE E: PASS — concurrent ranges fully isolated; teardown is per-range"
}

phaseA && phaseB && phaseD && phaseE && phaseC
echo
echo "================ DONE =================="
