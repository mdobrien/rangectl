# rangectl CLI Reference

> **Audience: coding agents.** Day-2 ops on ranges deployed via the SDK. The CLI
> does NOT deploy — the SDK (Range subclass + `deploy()`) does. The CLI connects
> to running ranges and drives them. Entry: `python -m rangectl <cmd>` (or
> `rangectl <cmd>` if installed). Module: `rangectl/cli.py` (Phase 14).

## Execution model (read first)

- Ranges are **per-host** (libvirt/QEMU + netns). The CLI reads the local
  `StateDB` (`~/.rangectl/rangectl.db`) + `/ranges/<name>/range.json`.
- On a non-KVM host (e.g. the Mac) `list` is always empty — run on the **EC2**
  box where ranges live. See `ec2-usage.md`.
- Namespace-mode ranges need **root**: `sudo python3 -m rangectl ...` on EC2.
- The CLI is thin — every command is `Range.connect(name)` → SDK call → format.
  Don't add infra logic to the CLI; fix the SDK.

## Exit codes

`0` ok · `1` error · `2` range/node not found (`RangeNotRunning` or unknown
node). `exec` passes through the **remote command's** exit code.

## Commands

| Command | Notes |
|---|---|
| `list` | ranges + status (running/frozen/orphaned), node count, subnet |
| `status <range> [--yaml]` | per-node: status, IP, image, OS, vcpu, mem. `--yaml` for scripting |
| `exec <range> <node> -- <cmd...>` | SSH exec; stdout/stderr passthrough; returns remote exit code |
| `exec <range> <node> -i` | interactive SSH (`os.execvp ssh`, per-range key) |
| `upload <range> <node> <src> <dst>` | SFTP upload |
| `ssh-config <range>` | SSH config block per node (key `~/.rangectl/keys/<range>/id_ed25519`) |
| `node <range> <node> {stop,start,restart,status}` | VM power; `status` prints virsh domstate |
| `virsh <range> <args...>` | virsh scoped to the range's libvirt socket (`os.execvp`) |
| `netns <range> -- <cmd...>` | `ip netns exec rangectl-<range> <cmd>` |
| `logs <range> [--node N] [--level L]` | DB log events |
| `net <range>` | netns, mgmt subnet, veth, node IPs, bridges |
| `ps <range>` | `pstree -p <libvirtd-pid>` from range.json |
| `freeze` / `thaw <range>` | cgroup freeze/resume (ns mode only) |
| `snapshot` / `restore <range> <name>` | topology-wide |
| `internet <range> {full,none}` | outbound NAT toggle (ns mode only) |
| `destroy <range>` | connect+destroy; falls back to `cleanup` if not running |
| `destroy --all` | destroy every range |
| `cleanup <range>` | force-remove orphaned state |
| `images {list, add <name> <path> [--inject M] [--os-type T], remove <name>, info <name>}` | StateDB image registry |

## Teardown / cleanup (range-scoped — read this)

To kill a range, use the CLI: `rangectl destroy <range>` (idempotent — falls
back to `cleanup` if already gone) or `rangectl cleanup <range>` for orphans.
Both are **range-scoped**: they kill only that range's libvirtd wrapper PID
(from `range.json`) + cgroup; the kernel reaps that range's QEMU. `destroy
--all` does this per range.

**NEVER** clean up with host-wide kills — `pkill -f qemu-system`,
`pkill -f libvirtd`, blanket `ip netns del` loops. They reach across ranges and
SIGTERM other agents' VMs mid-deploy (this happened: a blanket pkill in a test
pre-clean killed a concurrent benchmark range). Tests/scripts must tear down
their **own** range by name via the CLI.

## Gotchas

- `arg ranges/nodes` resolve via the default DB — wrong host = "not found".
- `exec` requires SSH-capable node (Linux/VyOS post-bootstrap). VyOS
  pre-bootstrap / Windows error out; use `virsh <range> console <node>`.
- `destroy` on a stale/orphaned range auto-routes to `cleanup` (idempotent).
- Not implemented (deferred, SDK covers): `deploy <yaml>`, `logs --follow`,
  `qemu-log`. Don't assume they exist.

## SDK parity added for the CLI

`LiveNode.status` (property) + `LibvirtBackend.status(vm_id)` back
`node ... status`. Power ops also on the SDK: `rng["n"].stop/start/restart`,
`rng["n"].status`.
