"""tc netem command builders for link impairment (Phase 19).

Pure functions — no side effects, no I/O. Each builder returns a list of
argv-style command lists ready to hand to the backend's tc runner. The
``netns`` argument, when set, prefixes every command with ``ip netns exec
<netns>`` so the rules land inside the range's network namespace.

netem lives on the egress side of a TAP, so impairing one TAP degrades the
traffic that VM sends out. Bandwidth limiting needs a ``tbf`` qdisc as the
root with netem hung underneath it as a child — netem alone cannot rate-limit.
"""
from __future__ import annotations


def _prefix(netns: str | None) -> list[str]:
    return ["ip", "netns", "exec", netns] if netns else []


def _netem_args(latency, jitter, loss, reorder, corrupt, duplicate) -> list[str]:
    args: list[str] = []
    # reorder is only meaningful with a delay; inject a small base delay so the
    # tc command doesn't fail when reorder is requested on its own.
    if latency is None and reorder is not None:
        latency = "10ms"
    if latency is not None:
        args += ["delay", latency]
        if jitter is not None:
            args.append(jitter)
    if loss is not None:
        args += ["loss", loss]
    if duplicate is not None:
        args += ["duplicate", duplicate]
    if corrupt is not None:
        args += ["corrupt", corrupt]
    if reorder is not None:
        args += ["reorder", reorder]
    return args


def build_netem_cmds(tap: str, netns: str | None, *,
                     latency=None, jitter=None, bandwidth=None,
                     loss=None, reorder=None, corrupt=None,
                     duplicate=None) -> list[list[str]]:
    """Return the tc commands that apply the given impairments on ``tap``."""
    pre = _prefix(netns)
    netem = _netem_args(latency, jitter, loss, reorder, corrupt, duplicate)
    cmds: list[list[str]] = []
    if bandwidth is not None:
        # tbf at the root enforces the rate; netem (if any) hangs under it.
        cmds.append(pre + ["tc", "qdisc", "replace", "dev", tap, "root",
                           "handle", "1:", "tbf", "rate", bandwidth,
                           "burst", "32kbit", "latency", "50ms"])
        if netem:
            cmds.append(pre + ["tc", "qdisc", "replace", "dev", tap,
                               "parent", "1:1", "handle", "10:", "netem"]
                        + netem)
    else:
        cmds.append(pre + ["tc", "qdisc", "replace", "dev", tap, "root",
                           "netem"] + netem)
    return cmds


def build_clear_cmds(tap: str, netns: str | None) -> list[list[str]]:
    """Return the tc command that removes all qdiscs from ``tap``."""
    return [_prefix(netns) + ["tc", "qdisc", "del", "dev", tap, "root"]]
