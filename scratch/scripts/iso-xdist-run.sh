#!/usr/bin/env bash
# Checkpoint C: the real acceptance — bounded-concurrency integration via xdist.
#
# Differences from the 12-at-once harness (iso-concurrent-run.sh):
#  - -n N + --dist loadfile: at most N files run at once (each file's tests
#    serial on one worker), instead of all 13 files (~35 VMs) at once.
#  - --timeout via pytest-timeout (in-process) NOT shell `timeout` — a stuck
#    test raises inside pytest so fixture teardown still runs and the range is
#    destroyed. This avoids the orphaned-libvirtd/qemu leak the shell-kill
#    harness produced.
#  - RANGECTL_RANGE_PREFIX/STATE_ROOT per worker (set in conftest below) keep
#    same-named reruns isolated; the flock registry hands out distinct subnets.
set -u
cd /home/ubuntu/rangectl

N="${1:-4}"
export RANGECTL_SUBNET_REGISTRY=/run/rangectl/mgmt_subnets.json

# Fresh registry so a prior batch's stale entries don't shrink the pool.
sudo mkdir -p /run/rangectl
sudo find /run/rangectl -maxdepth 1 -name 'mgmt_subnets.json' -delete 2>/dev/null
echo "subnet registry reset; running with -n $N --dist loadfile"

echo "=== PRE-RUN host state ==="
echo "qemu=$(ps aux|grep qemu-system|grep -v grep|wc -l) libvirtd=$(ps aux|grep 'libvirtd --config'|grep -v grep|wc -l) netns=$(ip netns list 2>/dev/null|grep -c rangectl)"

START=$(date +%s)
sudo env RANGECTL_SUBNET_REGISTRY="$RANGECTL_SUBNET_REGISTRY" \
     python3 -m pytest tests/integration \
     -n "$N" --dist loadfile \
     --timeout=900 --timeout-method=thread \
     -p no:cacheprovider -q 2>&1
RC=$?
END=$(date +%s)
echo "=== xdist RC=$RC  WALL=$((END-START))s ==="

echo "=== POST-RUN leak inventory (in-process timeout => teardown ran => should be 0) ==="
echo "qemu=$(ps aux|grep qemu-system|grep -v grep|wc -l) libvirtd=$(ps aux|grep 'libvirtd --config'|grep -v grep|wc -l) netns=$(ip netns list 2>/dev/null|grep -c rangectl) veth=$(ip link show 2>/dev/null|grep -cE 'mgh|mgp|rlmgt')"
echo "--registry (should be empty after all frees)--"
sudo cat /run/rangectl/mgmt_subnets.json 2>/dev/null; echo
echo "=== DONE ==="
