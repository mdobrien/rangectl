#!/usr/bin/env bash
# Parallel test-isolation experiment: run each integration test file as its own
# concurrent pytest process and capture per-file results + host leak inventory.
# Read-only analysis harness — does NOT modify rangectl. Cleans up after itself.
set -u
cd /home/ubuntu/rangectl

LOGDIR=/tmp/iso-run
sudo rm -f "$LOGDIR"/*.log 2>/dev/null
mkdir -p "$LOGDIR"

# Reset the host-global subnet registry so a prior crashed/timed-out batch's
# stale entries don't shrink the pool. Ranges free their /24 on teardown.
sudo mkdir -p /run/rangectl
sudo find /run/rangectl -maxdepth 1 -name 'mgmt_subnets.json' -delete 2>/dev/null
echo "subnet registry reset"

echo "=== PRE-RUN host state ==="
echo "qemu=$(ps aux | grep qemu-system | grep -v grep | wc -l) libvirtd=$(ps aux | grep 'libvirtd --config' | grep -v grep | wc -l) netns=$(ip netns list 2>/dev/null | grep -c rangectl) veth=$(ip link show 2>/dev/null | grep -cE 'mgh|mgp')"

echo "=== LAUNCH concurrent (one pytest per file) ==="
START=$(date +%s)
declare -a PIDS
for f in tests/integration/test_*.py; do
  b=$(basename "$f" .py)
  sudo timeout 900 python3 -m pytest "$f" -p no:cacheprovider -q > "$LOGDIR/$b.log" 2>&1 &
  PIDS+=("$!:$b")
  echo "launched $b pid=$!"
done

echo "=== WAIT ==="
for entry in "${PIDS[@]}"; do
  pid="${entry%%:*}"; name="${entry##*:}"
  wait "$pid"; rc=$?
  echo "DONE $name rc=$rc"
done
END=$(date +%s)
echo "=== WALL CLOCK: $((END-START))s ==="

echo "=== RESULTS SUMMARY ==="
for f in "$LOGDIR"/test_*.log; do
  b=$(basename "$f")
  line=$(grep -E "[0-9]+ (passed|failed|error)" "$f" | tail -1)
  echo "$b :: ${line:-NO_SUMMARY_LINE}"
done

echo "=== POST-RUN host leak inventory ==="
echo "--qemu procs--";        ps aux | grep qemu-system | grep -v grep | wc -l
echo "--libvirtd procs--";    ps aux | grep 'libvirtd --config' | grep -v grep | wc -l
echo "--netns--";             ip netns list 2>/dev/null | grep rangectl
echo "--veth mgh/mgp--";      ip link show 2>/dev/null | grep -E 'mgh|mgp' | wc -l
echo "--/ranges dirs--";      ls /ranges/ 2>/dev/null
echo "--overlays--";          ls /var/lib/libvirt/images/rangectl/overlays/ 2>/dev/null
echo "--seeds--";             ls /var/lib/libvirt/images/rangectl/seeds/ 2>/dev/null
echo "--mgmt .254 addrs on host (subnet collision evidence)--"
ip -o -4 addr show 2>/dev/null | grep -E 'mgh|rlmgt' | awk '{print $2, $4}'
echo "--distinct host routes to mgmt /24s (should be 1 route per /24)--"
ip route show 2>/dev/null | grep -oE '192\.168\.1[0-9][0-9]\.0/24' | sort | uniq -c
echo "--subnet registry contents--"
sudo cat /run/rangectl/mgmt_subnets.json 2>/dev/null || echo "(empty/cleared)"
echo "=== DONE ==="
