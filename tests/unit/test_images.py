from __future__ import annotations

from pathlib import Path

import pytest

from rangectl.images import ImageBuilder, ImageRegistry
from rangectl.types import InjectMethod


def _make_dummy_qcow(tmp_path: Path, name: str = "src.qcow2", size: int = 1024) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_registry_add_copies_file(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))

    registry.add("img1", str(src))

    dest = storage / "img1.qcow2"
    assert dest.exists()
    assert dest.read_bytes() == src.read_bytes()


def test_registry_add_records_in_db(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path, size=2 * 1024 * 1024)
    registry = ImageRegistry(db, storage_path=str(storage))

    registry.add("img1", str(src), inject=InjectMethod.CLOUD_INIT, os_type="linux")

    rec = db.get_image("img1")
    assert rec is not None
    assert rec["name"] == "img1"
    assert rec["path"] == str(storage / "img1.qcow2")
    assert rec["inject"] == "cloud-init"
    assert rec["os_type"] == "linux"
    assert rec["size_mb"] == 2


def test_registry_add_accepts_string_inject(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))

    registry.add("img1", str(src), inject="pre-baked")
    rec = db.get_image("img1")
    assert rec["inject"] == "pre-baked"


def test_registry_remove_deletes_file(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))
    registry.add("img1", str(src))
    dest = storage / "img1.qcow2"
    assert dest.exists()

    registry.remove("img1")

    assert not dest.exists()


def test_registry_remove_deletes_from_db(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))
    registry.add("img1", str(src))

    registry.remove("img1")

    assert db.get_image("img1") is None


def test_registry_remove_nonexistent_is_safe(db, tmp_path):
    storage = tmp_path / "storage"
    registry = ImageRegistry(db, storage_path=str(storage))
    # should not raise
    registry.remove("nope")


def test_registry_list(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))

    registry.add("img1", str(src))
    registry.add("img2", str(src))

    items = registry.list()
    names = {r["name"] for r in items}
    assert names == {"img1", "img2"}


def test_registry_exists(db, tmp_path):
    storage = tmp_path / "storage"
    src = _make_dummy_qcow(tmp_path)
    registry = ImageRegistry(db, storage_path=str(storage))

    assert not registry.exists("img1")
    registry.add("img1", str(src))
    assert registry.exists("img1")
    registry.remove("img1")
    assert not registry.exists("img1")


def test_image_builder_collects_deps():
    builder = ImageBuilder(base_image="ubuntu-22.04")
    builder.packages(["nginx", "curl"])
    builder.run("echo hello")
    builder.install(name="agent", src="/local/agent.tar", install_cmd="./install.sh")

    @builder.configure
    def _setup(node):
        node.exec("setup")

    assert builder._packages == ["nginx", "curl"]
    assert builder._run_commands == ["echo hello"]
    assert len(builder._installs) == 1
    assert builder._installs[0].name == "agent"
    assert len(builder._configure_fns) == 1
    assert builder._configure_fns[0].__name__ == "_setup"
