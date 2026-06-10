"""tc command builders for port mirroring (Phase 21, D4).

Pure functions, mirroring link_properties.py: each builder returns argv-style
command lists for the backend's tc runner. clsact provides both ingress and
egress filter hooks and occupies a separate slot from the root qdisc, so
mirrors coexist with Phase 19 netem/tbf impairments on the same device.

Install is idempotent by delete-then-add: ``tc filter add`` appends (re-apply
would stack duplicate filters) and a second ``clsact add`` errors with
"Exclusivity flag on". run_tc tolerates the del failing when clsact is absent.
"""
from __future__ import annotations

VALID_DIRECTIONS = ("ingress", "egress", "both")


def _prefix(netns: str | None) -> list[str]:
    return ["ip", "netns", "exec", netns] if netns else []


def _hooks(direction: str) -> list[str]:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}")
    return ["ingress", "egress"] if direction == "both" else [direction]


def build_mirror_cmds(src_dev: str, dst_dev: str, direction: str,
                      netns: str | None = None) -> list[list[str]]:
    """tc commands that mirror ``src_dev``'s traffic to ``dst_dev``."""
    hooks = _hooks(direction)
    pre = _prefix(netns)
    cmds = [
        pre + ["tc", "qdisc", "del", "dev", src_dev, "clsact"],
        pre + ["tc", "qdisc", "add", "dev", src_dev, "clsact"],
    ]
    for hook in hooks:
        cmds.append(pre + ["tc", "filter", "add", "dev", src_dev, hook,
                           "matchall", "action", "mirred", "egress",
                           "mirror", "dev", dst_dev])
    return cmds


def build_unmirror_cmds(src_dev: str,
                        netns: str | None = None) -> list[list[str]]:
    """Removing clsact drops all its filters in one shot."""
    return [_prefix(netns) + ["tc", "qdisc", "del", "dev", src_dev, "clsact"]]
