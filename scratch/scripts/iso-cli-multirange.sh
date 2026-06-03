#!/usr/bin/env bash
# Product test: deploy N ranges CONCURRENTLY (each its own process, like a user),
# then manage them entirely through the rangectl CLI (list / exec / destroy).
# Proves concurrent multi-range works end-to-end with clean CLI teardown.
set -u
cd /home/ubuntu/rangectl
export PYTHONPATH=/home/ubuntu/rangectl
N="${1:-4}"
NAMES=()
for i in $(seq 1 "$N"); do NAMES+=("iso-cli-$i"); done

RANGECTL="python3 -m rangectl.cli"

echo "=== PRE: reset prod DB + registry, verify clean ==="
sudo find /root/.rangectl -maxdepth 1 -name 'rangectl.db*' -delete 2>/dev/null
sudo find /root/.rangectl -maxdepth 1 -name 'mgmt_subnets.json' -delete 2>/dev/null
echo "baseline: qemu=$(ps aux|grep '[q]emu-system'|wc -l) libvirtd=$(ps aux|grep '[l]ibvirtd --config'|wc -l) netns=$(ip netns list 2>/dev/null|grep -c rangectl)"

echo "=== DEPLOY $N ranges concurrently ==="
T0=$(date +%s)
pids=()
for name in "${NAMES[@]}"; do
  sudo env PYTHONPATH=/home/ubuntu/rangectl python3 scratch/scripts/iso-cli-deploy.py "$name" > "/tmp/${name}.deploy.log" 2>&1 &
  pids+=("$!")
  sleep 5   # 5s stagger between range deploys (spread the boot I/O)
done
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=$((fail+1)); done
T1=$(date +%s)
echo "all deploys returned in $((T1-T0))s (failed=$fail)"
for name in "${NAMES[@]}"; do echo "  $(grep -hE 'DEPLOYED|Error|Traceback' /tmp/${name}.deploy.log | tail -1)"; done

echo "=== CLI: list ==="
sudo $RANGECTL list

echo "=== CLI: exec hostname + intra-range ping on each range ==="
ok=0
for name in "${NAMES[@]}"; do
  hn=$(sudo $RANGECTL exec "$name" a -- hostname 2>&1 | tr -d '\r')
  pg=$(sudo $RANGECTL exec "$name" a -- ping -c2 -W2 10.0.5.2 2>&1 | grep -oE '[0-9]+ received' | head -1)
  echo "  $name: hostname='$hn' ping='$pg'"
  [[ "$hn" == *"$name-a"* ]] && [[ "$pg" == "2 received" ]] && ok=$((ok+1))
done
echo "ranges fully working: $ok/$N"

echo "=== CLI: destroy --all ==="
T2=$(date +%s)
sudo $RANGECTL destroy --all
T3=$(date +%s)
echo "destroy --all took $((T3-T2))s"

sleep 3
echo "=== POST: leak check ==="
echo "qemu=$(ps aux|grep '[q]emu-system'|wc -l) libvirtd=$(ps aux|grep '[l]ibvirtd --config'|wc -l) netns=$(ip netns list 2>/dev/null|grep -c rangectl) veth=$(ip link show 2>/dev/null|grep -cE 'mgh|rlmgt') ranges_dir=$(ls /ranges 2>/dev/null|wc -l)"
echo "registry: $(sudo cat /root/.rangectl/mgmt_subnets.json 2>/dev/null || echo empty)"
echo "=== SUMMARY: deploy=$((T1-T0))s working=$ok/$N destroy=$((T3-T2))s ==="
