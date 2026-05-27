from __future__ import annotations
import logging
import shutil
from pathlib import Path

from rangectl.dependencies import DependencyMixin
from rangectl.state import StateDB
from rangectl.types import InjectMethod

log = logging.getLogger(__name__)


class ImageRegistry:

    def __init__(self, db: StateDB, storage_path: str = "~/.rangectl/images") -> None:
        self._db = db
        self._storage_path = Path(storage_path).expanduser()
        self._storage_path.mkdir(parents=True, exist_ok=True)
        log.info("ImageRegistry initialized at %s", self._storage_path)

    def add(
        self,
        name: str,
        path: str,
        inject: InjectMethod | str = InjectMethod.PRE_BAKED,
        os_type: str = "linux",
    ) -> None:
        inject_str = inject.value if isinstance(inject, InjectMethod) else inject
        log.info("Registering image: %s from %s (inject=%s)", name, path, inject_str)
        src = Path(path)
        dest = self._storage_path / f"{name}.qcow2"
        shutil.copy2(str(src), str(dest))
        size_mb = dest.stat().st_size // (1024 * 1024)
        self._db.add_image(name, str(dest), inject_str, os_type, size_mb)

    def remove(self, name: str) -> None:
        log.info("Removing image: %s", name)
        record = self._db.get_image(name)
        if record:
            Path(record["path"]).unlink(missing_ok=True)
            self._db.remove_image(name)

    def list(self) -> list[dict]:
        log.info("Listing images")
        return self._db.list_images()

    def get(self, name: str) -> dict | None:
        log.info("Getting image: %s", name)
        return self._db.get_image(name)

    def exists(self, name: str) -> bool:
        return self._db.image_exists(name)


class ImageBuilder(DependencyMixin):

    def __init__(self, base_image: str) -> None:
        super().__init__()
        self.base_image = base_image
        self._run_commands: list[str] = []
        log.info("ImageBuilder initialized with base: %s", base_image)

    def packages(self, packages: list[str]) -> ImageBuilder:
        self._packages.extend(packages)
        return self

    def run(self, command: str) -> ImageBuilder:
        log.info("ImageBuilder adding run command: %s", command)
        self._run_commands.append(command)
        return self

    def build(self, name: str) -> str:
        log.info("Building image: %s from base %s", name, self.base_image)
        log.info("  packages: %s", self._packages)
        log.info("  installs: %s", [i.name for i in self._installs])
        log.info("  run commands: %s", self._run_commands)
        log.info("  configure fns: %s", [f.__name__ for f in self._configure_fns])
        log.info("  files: %s", self._files)
        # boot base image, apply all changes, snapshot, register in DB as pre-baked
        raise NotImplementedError
