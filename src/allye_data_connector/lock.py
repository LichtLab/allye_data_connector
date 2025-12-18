from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileLock:
    path: Path

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")  # noqa: SIM115
        _lock_file(self._fh)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            _unlock_file(self._fh)
        finally:
            self._fh.close()


def _lock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
