"""Unit tests for the rangectl CLI (Phase 14).

The CLI wraps the SDK. These tests mock ``Range.connect``/``Range.list`` and the
StateDB so no real VMs or infrastructure are needed — they exercise arg parsing,
output formatting, exit codes, and error handling.
"""
from __future__ import annotations

import pytest

from rangectl import cli
from rangectl.types import ExecResult, RangeNotRunning


# --- fakes -----------------------------------------------------------------

class FakeNode:
    def __init__(self, name, mgmt_ip="192.168.100.2", ssh_user="ubuntu",
                 status="running", exec_result=None):
        self.name = name
        self.mgmt_ip = mgmt_ip
        self.ssh_user = ssh_user
        self.status = status
        self._exec_result = exec_result or ExecResult(0, "", "")
        self.calls = []

    def exec(self, cmd):
        self.calls.append(("exec", cmd))
        return self._exec_result

    def upload(self, src, dst):
        self.calls.append(("upload", src, dst))

    def stop(self):
        self.calls.append(("stop",))

    def start(self):
        self.calls.append(("start",))

    def restart(self):
        self.calls.append(("restart",))


class FakeDB:
    def __init__(self, nodes=None):
        self._nodes = nodes or []

    def list_nodes(self, name):
        return self._nodes


class FakeRange:
    def __init__(self, name, nodes):
        self.name = name
        self._nodes = {n.name: n for n in nodes}
        self._db = FakeDB([
            {"name": n.name, "image": "ubuntu-22.04", "os_type": "linux",
             "vcpu": 1, "memory_mb": 1024, "mgmt_ip": n.mgmt_ip,
             "state": "running"}
            for n in nodes
        ])
        self.destroyed = False
        self.frozen = None
        self.snapshots = []
        self.restores = []
        self.internet = "none"

    def __getitem__(self, node_name):
        return self._nodes[node_name]

    def destroy(self):
        self.destroyed = True

    def freeze(self):
        self.frozen = True

    def thaw(self):
        self.frozen = False

    def snapshot(self, name):
        self.snapshots.append(name)

    def restore(self, name):
        self.restores.append(name)

    def enable_internet(self):
        self.internet = "full"

    def disable_internet(self):
        self.internet = "none"


def _run(argv):
    return cli.main(argv)


# --- arg parsing -----------------------------------------------------------

def test_parser_builds():
    parser = cli.build_parser()
    args = parser.parse_args(["list"])
    assert args.func is cli.cmd_list


def test_no_command_prints_help():
    assert cli.main([]) == 1


def test_exec_parses_command_after_dashdash():
    parser = cli.build_parser()
    args = parser.parse_args(["exec", "lab", "router", "--", "ip", "addr"])
    assert args.range == "lab"
    assert args.node == "router"
    assert args.command == ["ip", "addr"]


def test_node_subaction_parses():
    parser = cli.build_parser()
    args = parser.parse_args(["node", "lab", "router", "stop"])
    assert args.action == "stop"


# --- list ------------------------------------------------------------------

def test_list_empty(monkeypatch, capsys):
    monkeypatch.setattr(cli.Range, "list", staticmethod(lambda: []))
    assert _run(["list"]) == 0
    assert "No ranges" in capsys.readouterr().out


def test_list_formats_table(monkeypatch, capsys):
    monkeypatch.setattr(cli.Range, "list", staticmethod(lambda: [
        {"name": "lab", "status": "running", "node_count": 2,
         "mgmt_subnet": "192.168.100.0/24", "created_at": "2026-06-01"},
    ]))
    assert _run(["list"]) == 0
    out = capsys.readouterr().out
    assert "lab" in out
    assert "running" in out
    assert "192.168.100.0/24" in out
    assert "2" in out


# --- status ----------------------------------------------------------------

def test_status_table(monkeypatch, capsys):
    rng = FakeRange("lab", [FakeNode("router"), FakeNode("target")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["status", "lab"]) == 0
    out = capsys.readouterr().out
    assert "router" in out and "target" in out
    assert "ubuntu-22.04" in out


def test_status_yaml(monkeypatch, capsys):
    rng = FakeRange("lab", [FakeNode("router")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["status", "lab", "--yaml"]) == 0
    out = capsys.readouterr().out
    assert "router" in out
    # YAML uses key: value form
    assert "image:" in out or "name:" in out


def test_status_range_not_found(monkeypatch, capsys):
    def boom(n):
        raise RangeNotRunning(n, "no such range")
    monkeypatch.setattr(cli.Range, "connect", staticmethod(boom))
    assert _run(["status", "ghost"]) == 2
    assert "ghost" in capsys.readouterr().err


# --- exec ------------------------------------------------------------------

def test_exec_passthrough_stdout_and_exit(monkeypatch, capsys):
    node = FakeNode("router", exec_result=ExecResult(0, "hello\n", ""))
    rng = FakeRange("lab", [node])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    rc = _run(["exec", "lab", "router", "--", "echo", "hello"])
    assert rc == 0
    assert capsys.readouterr().out == "hello\n"
    assert node.calls == [("exec", "echo hello")]


def test_exec_returns_remote_exit_code(monkeypatch, capsys):
    node = FakeNode("router", exec_result=ExecResult(3, "", "boom\n"))
    rng = FakeRange("lab", [node])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    rc = _run(["exec", "lab", "router", "--", "false"])
    assert rc == 3
    assert capsys.readouterr().err == "boom\n"


def test_exec_unknown_node(monkeypatch, capsys):
    rng = FakeRange("lab", [FakeNode("router")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    rc = _run(["exec", "lab", "ghost", "--", "ls"])
    assert rc == 2
    assert "ghost" in capsys.readouterr().err


# --- upload ----------------------------------------------------------------

def test_upload(monkeypatch, capsys):
    node = FakeNode("router")
    rng = FakeRange("lab", [node])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["upload", "lab", "router", "/tmp/a", "/tmp/b"]) == 0
    assert node.calls == [("upload", "/tmp/a", "/tmp/b")]


# --- ssh-config ------------------------------------------------------------

def test_ssh_config_output(monkeypatch, capsys):
    rng = FakeRange("lab", [
        FakeNode("router", mgmt_ip="192.168.100.1"),
        FakeNode("target", mgmt_ip="192.168.100.2"),
    ])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["ssh-config", "lab"]) == 0
    out = capsys.readouterr().out
    assert "Host lab-router" in out
    assert "HostName 192.168.100.1" in out
    assert "User ubuntu" in out
    assert "id_ed25519" in out
    assert "lab/id_ed25519" in out


# --- node power ------------------------------------------------------------

def test_node_stop(monkeypatch, capsys):
    node = FakeNode("router")
    rng = FakeRange("lab", [node])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["node", "lab", "router", "stop"]) == 0
    assert ("stop",) in node.calls


def test_node_status(monkeypatch, capsys):
    node = FakeNode("router", status="shut off")
    rng = FakeRange("lab", [node])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["node", "lab", "router", "status"]) == 0
    assert "shut off" in capsys.readouterr().out


# --- lifecycle -------------------------------------------------------------

def test_freeze(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["freeze", "lab"]) == 0
    assert rng.frozen is True


def test_thaw(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["thaw", "lab"]) == 0
    assert rng.frozen is False


def test_snapshot(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["snapshot", "lab", "clean"]) == 0
    assert rng.snapshots == ["clean"]


def test_restore(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["restore", "lab", "clean"]) == 0
    assert rng.restores == ["clean"]


def test_internet_full(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["internet", "lab", "full"]) == 0
    assert rng.internet == "full"


def test_internet_none(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    rng.internet = "full"
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["internet", "lab", "none"]) == 0
    assert rng.internet == "none"


def test_destroy(monkeypatch):
    rng = FakeRange("lab", [FakeNode("a")])
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["destroy", "lab"]) == 0
    assert rng.destroyed is True


def test_destroy_orphaned_suggests_cleanup(monkeypatch, capsys):
    def boom(n):
        raise RangeNotRunning(n, "orphaned")
    monkeypatch.setattr(cli.Range, "connect", staticmethod(boom))
    called = {}
    monkeypatch.setattr(cli.Range, "cleanup",
                        staticmethod(lambda n: called.setdefault("n", n)))
    # destroy on a non-running range falls back to cleanup.
    assert _run(["destroy", "ghost"]) == 0
    assert called["n"] == "ghost"


def test_cleanup(monkeypatch):
    called = {}
    monkeypatch.setattr(cli.Range, "cleanup",
                        staticmethod(lambda n: called.setdefault("n", n)))
    assert _run(["cleanup", "lab"]) == 0
    assert called["n"] == "lab"


# --- logs ------------------------------------------------------------------

def test_logs(monkeypatch, capsys):
    rng = FakeRange("lab", [FakeNode("a")])
    rng.logs = lambda level=None: [
        {"timestamp": "t", "node_name": "a", "level": "info", "message": "hi"},
    ]
    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: rng))
    assert _run(["logs", "lab"]) == 0
    assert "hi" in capsys.readouterr().out


# --- images ----------------------------------------------------------------

class FakeImageDB:
    def __init__(self):
        self.images = {}
        self.closed = False

    def list_images(self):
        return list(self.images.values())

    def get_image(self, name):
        return self.images.get(name)

    def add_image(self, name, path, inject="pre-baked", os_type="linux",
                  size_mb=None, built_from=None):
        self.images[name] = {"name": name, "path": path, "inject": inject,
                             "os_type": os_type, "size_mb": size_mb}

    def remove_image(self, name):
        self.images.pop(name, None)

    def close(self):
        self.closed = True


def test_images_list_empty(monkeypatch, capsys):
    db = FakeImageDB()
    monkeypatch.setattr(cli, "StateDB", lambda *a, **k: db)
    assert _run(["images", "list"]) == 0
    assert "No images" in capsys.readouterr().out


def test_images_add_and_list(monkeypatch, capsys, tmp_path):
    db = FakeImageDB()
    monkeypatch.setattr(cli, "StateDB", lambda *a, **k: db)
    img = tmp_path / "base.qcow2"
    img.write_bytes(b"x" * (2 * 1024 * 1024))
    assert _run(["images", "add", "kali", str(img),
                 "--inject", "cloud-init", "--os-type", "linux"]) == 0
    assert "kali" in db.images
    assert db.images["kali"]["inject"] == "cloud-init"
    assert _run(["images", "list"]) == 0
    assert "kali" in capsys.readouterr().out


def test_images_info(monkeypatch, capsys):
    db = FakeImageDB()
    db.images["kali"] = {"name": "kali", "path": "/x.qcow2",
                         "inject": "cloud-init", "os_type": "linux",
                         "size_mb": 2}
    monkeypatch.setattr(cli, "StateDB", lambda *a, **k: db)
    assert _run(["images", "info", "kali"]) == 0
    out = capsys.readouterr().out
    assert "kali" in out and "/x.qcow2" in out


def test_images_info_missing(monkeypatch, capsys):
    db = FakeImageDB()
    monkeypatch.setattr(cli, "StateDB", lambda *a, **k: db)
    assert _run(["images", "info", "ghost"]) == 1
    assert "ghost" in capsys.readouterr().err


def test_images_remove(monkeypatch):
    db = FakeImageDB()
    db.images["kali"] = {"name": "kali"}
    monkeypatch.setattr(cli, "StateDB", lambda *a, **k: db)
    assert _run(["images", "remove", "kali"]) == 0
    assert "kali" not in db.images
