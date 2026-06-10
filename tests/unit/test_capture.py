"""Unit tests for Phase 21 — packet capture (rangectl/capture.py).

Design: scratch/issues/20260609-14-phase21-pcap-mirror-design.md.
tcpdump is spawned inside the range's net+PID+mount namespaces via nsenter on
the libvirtd PID, so the kernel reaps it with the range (D2-B). Device
resolution reuses Phase 20's LinkEndpoint — no new lookup path (D3).
"""
from __future__ import annotations

import signal

import pytest

from rangectl import capture as capture_mod
from rangectl.capture import Capture, build_capture_cmd
from rangectl.engine import Engine
from rangectl.topology import Topology


# --- nsenter command construction (pure) -------------------------------------

def test_build_capture_cmd_enters_all_three_namespaces():
    cmd = build_capture_cmd(4242, "vnet0", "/tmp/x.pcap")
    # net + PID + mount, exactly as the design's corrected D2-B specifies.
    assert cmd[:7] == ["nsenter", "-t", "4242", "-n", "-p", "-m", "--"]
    assert "-n" in cmd and "-p" in cmd and "-m" in cmd


def test_build_capture_cmd_tcpdump_args():
    cmd = build_capture_cmd(1, "vnet0", "/ranges/lab/captures/cap-1.pcap")
    i = cmd.index("tcpdump")
    rest = cmd[i:]
    assert rest[rest.index("-i") + 1] == "vnet0"
    assert rest[rest.index("-w") + 1] == "/ranges/lab/captures/cap-1.pcap"
    # tcpdump drops privileges before opening -w; -Z root keeps the savefile
    # writable in the root-owned captures dir.
    assert rest[rest.index("-Z") + 1] == "root"
    # --immediate-mode + -U: per-packet delivery and savefile flush, so
    # stop()'s SIGTERM never drops the kernel-ring tail (Gate 2 root cause).
    assert "--immediate-mode" in rest
    assert "-U" in rest


def test_build_capture_cmd_bpf_filter_is_single_trailing_arg():
    cmd = build_capture_cmd(1, "vnet0", "/tmp/x.pcap", bpf="tcp port 80")
    assert cmd[-1] == "tcp port 80"


def test_build_capture_cmd_extra_args_pass_through():
    cmd = build_capture_cmd(1, "vnet0", "/tmp/x.pcap",
                            extra_args=["-C", "10", "-W", "3"])
    assert ["-C", "10", "-W", "3"] == cmd[-4:]


# --- Capture.stop() signal sequence -------------------------------------------

class _SignalRecorder:
    """Fake _kill/_pid_alive pair: process dies after `dies_after` TERMs."""

    def __init__(self, dies_on_term=True):
        self.signals: list[tuple[int, int]] = []
        self.alive = True
        self.dies_on_term = dies_on_term

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == signal.SIGTERM and self.dies_on_term:
            self.alive = False
        if sig == signal.SIGKILL:
            self.alive = False

    def pid_alive(self, pid):
        return self.alive


def _patched_capture(monkeypatch, rec, **kwargs):
    monkeypatch.setattr(capture_mod, "_kill", rec.kill)
    monkeypatch.setattr(capture_mod, "_pid_alive", rec.pid_alive)
    return Capture(id=1, file="/tmp/x.pcap", pid=999, **kwargs)


def test_stop_sigterm_clean_exit(monkeypatch):
    rec = _SignalRecorder(dies_on_term=True)
    cap = _patched_capture(monkeypatch, rec)
    cap.stop(grace=0.2)
    assert rec.signals == [(999, signal.SIGTERM)]
    assert cap.possibly_truncated is False
    assert cap.stopped


def test_stop_escalates_to_sigkill_and_flags_truncated(monkeypatch):
    rec = _SignalRecorder(dies_on_term=False)
    cap = _patched_capture(monkeypatch, rec)
    cap.stop(grace=0.2)
    assert rec.signals[0] == (999, signal.SIGTERM)
    assert rec.signals[-1] == (999, signal.SIGKILL)
    assert cap.possibly_truncated is True


def test_stop_process_already_gone(monkeypatch):
    def kill(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(capture_mod, "_kill", kill)
    cap = Capture(id=1, file="/tmp/x.pcap", pid=999)
    cap.stop(grace=0.2)  # no raise
    assert cap.stopped
    assert cap.possibly_truncated is False


def test_stop_is_idempotent(monkeypatch):
    rec = _SignalRecorder()
    cap = _patched_capture(monkeypatch, rec)
    cap.stop(grace=0.2)
    cap.stop(grace=0.2)
    assert rec.signals == [(999, signal.SIGTERM)]


def test_context_manager_stops_on_exit(monkeypatch):
    rec = _SignalRecorder()
    cap = _patched_capture(monkeypatch, rec)
    with cap as c:
        assert c is cap
    assert cap.stopped
    assert rec.signals == [(999, signal.SIGTERM)]


def test_packet_count_reads_pcap_with_tcpdump(monkeypatch, tmp_path):
    pcap = tmp_path / "x.pcap"
    pcap.write_bytes(b"")
    recorded = {}

    class _Res:
        returncode = 0
        stdout = "p1\np2\np3\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return _Res()

    monkeypatch.setattr(capture_mod.subprocess, "run", fake_run)
    cap = Capture(id=1, file=str(pcap), pid=999)
    assert cap.packet_count() == 3
    assert recorded["cmd"][0] == "tcpdump"
    assert "-r" in recorded["cmd"]


def test_packet_count_raises_on_unreadable_pcap(monkeypatch, tmp_path):
    class _Res:
        returncode = 1
        stdout = ""
        stderr = "bad dump file"

    monkeypatch.setattr(capture_mod.subprocess, "run",
                        lambda *a, **k: _Res())
    cap = Capture(id=1, file=str(tmp_path / "x.pcap"), pid=999)
    with pytest.raises(RuntimeError, match="bad dump file"):
        cap.packet_count()


# --- Range.capture() device resolution (LinkEndpoint reuse, D3) ---------------

def _deployed(backend, db, l2="switch"):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    b = t.node("b", image="ubuntu")
    dev = t.switch("core") if l2 == "switch" else t.hub("core")
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    t.link(a.eth2["10.0.2.1/24"], dev.port0)
    return Engine(backend, db).deploy(t)


def _spawns(backend):
    return backend.calls_of("spawn_capture")


def test_capture_vm_iface_resolves_tap_via_link_endpoint(backend, db, tmp_path):
    rng = _deployed(backend, db)
    cap = rng.capture("a", "eth1", output=str(tmp_path / "a.pcap"))
    (args, kwargs) = _spawns(backend)[0]
    # dev resolved through LinkEndpoint -> MockBackend._find_tap_for_mac.
    assert args[1].startswith("tap-vm-")
    assert backend.calls_of("_find_tap_for_mac")
    assert cap.file == str(tmp_path / "a.pcap")
    assert cap.pid > 0


def test_capture_l2_node_uses_bridge_device(backend, db, tmp_path):
    rng = _deployed(backend, db)
    rng.capture("core", output=str(tmp_path / "c.pcap"))
    (args, _) = _spawns(backend)[0]
    assert args[1] == "sw-core"


def test_capture_hub_uses_hub_bridge(backend, db, tmp_path):
    rng = _deployed(backend, db, l2="hub")
    rng.capture("core", output=str(tmp_path / "c.pcap"))
    (args, _) = _spawns(backend)[0]
    assert args[1] == "hub-core"


def test_capture_bridge_escape_hatch(backend, db, tmp_path):
    rng = _deployed(backend, db)
    rng.capture(bridge="data-0", output=str(tmp_path / "c.pcap"))
    (args, _) = _spawns(backend)[0]
    assert args[1] == "data-0"


def test_capture_bpf_filter_passed_through(backend, db, tmp_path):
    rng = _deployed(backend, db)
    rng.capture("a", "eth1", filter="tcp port 80",
                output=str(tmp_path / "a.pcap"))
    (_, kwargs) = _spawns(backend)[0]
    assert kwargs["bpf"] == "tcp port 80"


def test_capture_unknown_iface_names_valid_interfaces(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="eth1"):
        rng.capture("a", "eth9")


def test_capture_unknown_node_raises(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="nope"):
        rng.capture("nope", "eth1")


def test_capture_vm_node_requires_iface(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="interface"):
        rng.capture("a")


def test_capture_default_output_under_range_captures_dir(
        backend, db, tmp_path, monkeypatch):
    from rangectl import supervisor
    monkeypatch.setattr(supervisor, "DEFAULT_RANGE_DIR", str(tmp_path))
    rng = _deployed(backend, db)
    cap = rng.capture("a", "eth1")
    assert cap.file == str(tmp_path / "lab" / "captures" / f"cap-{cap.id}.pcap")
    assert (tmp_path / "lab" / "captures").is_dir()


# --- capture index (D5-B: DB stores intent/index, liveness is live) -----------

def test_capture_recorded_in_db_index(backend, db, tmp_path):
    rng = _deployed(backend, db)
    cap = rng.capture("a", "eth1", output=str(tmp_path / "a.pcap"))
    rows = db.list_captures("lab")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == cap.id
    assert row["node_name"] == "a"
    assert row["iface"] == "eth1"
    assert row["pid"] == cap.pid
    assert row["file"] == cap.file


def test_captures_listing_reads_live_process_state(
        backend, db, tmp_path, monkeypatch):
    rng = _deployed(backend, db)
    pcap = tmp_path / "a.pcap"
    pcap.write_bytes(b"")
    rng.capture("a", "eth1", output=str(pcap))
    monkeypatch.setattr(capture_mod, "_pid_alive", lambda pid: True)
    listed = rng.captures()
    assert listed[0]["status"] == "running"
    monkeypatch.setattr(capture_mod, "_pid_alive", lambda pid: False)
    listed = rng.captures()
    assert listed[0]["status"] == "stopped"
    assert listed[0]["file_exists"] is True


def test_stop_capture_by_id_cross_process(backend, db, tmp_path, monkeypatch):
    rng = _deployed(backend, db)
    cap = rng.capture("a", "eth1", output=str(tmp_path / "a.pcap"))
    rec = _SignalRecorder()
    monkeypatch.setattr(capture_mod, "_kill", rec.kill)
    monkeypatch.setattr(capture_mod, "_pid_alive", rec.pid_alive)
    stopped = rng.stop_capture(cap.id)
    assert rec.signals == [(cap.pid, signal.SIGTERM)]
    assert stopped.stopped


def test_stop_capture_unknown_id(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="no capture"):
        rng.stop_capture(99)
