"""Packet capture inside a range's namespaces (Phase 21, D1/D2-B).

tcpdump is spawned entering the range's net+PID+mount namespaces via
``nsenter -t <libvirtd-pid> -n -p -m``. Because the process lives in the
range's PID namespace, the kernel reaps it when the range dies — range destroy
needs NO capture-specific cleanup, and none should ever be added.

The pcap is written under ``/ranges/<name>/captures/`` which is host-visible
(the range mount namespace only binds libvirt paths), so the file outlives the
capture process for post-mortem reading until the range dir is removed.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

log = logging.getLogger(__name__)

DEFAULT_STOP_GRACE = 5.0

# Injectable for tests (signal sequencing is unit-tested without processes).
_kill = os.kill


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def build_capture_cmd(libvirtd_pid: int, dev: str, output: str,
                      bpf: str | None = None,
                      extra_args: list[str] | None = None) -> list[str]:
    """argv that runs tcpdump inside the range's net+PID+mount namespaces.

    ``-Z root``: tcpdump drops privileges before opening the savefile, which
    would fail in the root-owned captures dir. ``--immediate-mode -U``: hand
    each packet to tcpdump as it arrives and flush it straight to the
    savefile — without both, packets from the last ~1s sit in the kernel ring
    block when SIGTERM breaks the capture loop and are silently dropped
    (Gate 2 root-cause: the final echo-request was missing in every run).
    """
    cmd = ["nsenter", "-t", str(libvirtd_pid), "-n", "-p", "-m", "--",
           "tcpdump", "-i", dev, "-w", output, "-Z", "root",
           "--immediate-mode", "-U"]
    if extra_args:
        cmd += list(extra_args)
    if bpf:
        cmd.append(bpf)
    return cmd


class Capture:
    """Handle to a running (or stopped) capture.

    ``pid`` is tcpdump's host-visible PID (signals cross PID namespaces by
    host PID). ``possibly_truncated`` is set when stop() had to SIGKILL —
    tcpdump only flushes its packet buffer on SIGTERM.
    """

    def __init__(self, id: int, file: str, pid: int,
                 node: str | None = None, iface: str | None = None,
                 device: str | None = None, proc=None) -> None:
        self.id = id
        self.file = file
        self.pid = pid
        self.node = node
        self.iface = iface
        self.device = device
        self.possibly_truncated = False
        self.stopped = False
        self._proc = proc  # the nsenter wrapper, when spawned by this process

    def __repr__(self) -> str:
        return (f"Capture(id={self.id}, pid={self.pid}, file={self.file!r}, "
                f"stopped={self.stopped})")

    @property
    def running(self) -> bool:
        return not self.stopped and _pid_alive(self.pid)

    def stop(self, grace: float = DEFAULT_STOP_GRACE) -> None:
        """SIGTERM (tcpdump flushes and exits) -> up to ``grace`` seconds ->
        SIGKILL + possibly_truncated flag."""
        if self.stopped:
            return
        self.stopped = True
        log.info("Stopping capture %s (pid %d)", self.id, self.pid)
        try:
            _kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            log.info("Capture %s already exited", self.id)
        else:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and _pid_alive(self.pid):
                time.sleep(0.05)
            if _pid_alive(self.pid):
                log.warning("Capture %s did not exit in %.1fs; SIGKILL "
                            "(pcap possibly truncated)", self.id, grace)
                try:
                    _kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                else:
                    self.possibly_truncated = True
        if self._proc is not None:
            try:
                self._proc.wait(timeout=grace)
            except Exception:
                pass

    def packet_count(self) -> int:
        """Number of packets in the pcap, via ``tcpdump -r`` (D6's one allowed
        helper — every integration test needs it)."""
        res = subprocess.run(["tcpdump", "-n", "-r", self.file],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"cannot read pcap {self.file}: {res.stderr.strip()}")
        return len(res.stdout.splitlines())

    def __enter__(self) -> "Capture":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


def wait_for_child(parent_pid: int, timeout: float = 5.0) -> int:
    """Host PID of ``parent_pid``'s first child (nsenter forks tcpdump into
    the target PID namespace; the fork child is what stop() must signal)."""
    from rangectl.supervisor import _child_pids
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children = _child_pids(parent_pid)
        if children:
            return children[0]
        if not _pid_alive(parent_pid):
            break
        time.sleep(0.05)
    raise RuntimeError(
        f"capture process did not start (no child of pid {parent_pid})")
