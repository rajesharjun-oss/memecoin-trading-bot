"""Exclusive process ownership of paper workers for one SQLite journal."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class WorkerAlreadyRunningError(RuntimeError):
    """Another process already owns the paper workers for this journal."""


@contextmanager
def exclusive_worker_lock(state_path: Path) -> Iterator[None]:
    """Hold an OS file lock for the entire dashboard lifespan.

    The lock file contains no secrets. An OS lock is released on process death,
    including an abrupt termination, so a supervisor can safely restart it.
    """
    path = state_path.resolve().with_name(f"{state_path.name}.workers.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkerAlreadyRunningError(
                    "Paper workers already run for this state database"
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkerAlreadyRunningError(
                    "Paper workers already run for this state database"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
