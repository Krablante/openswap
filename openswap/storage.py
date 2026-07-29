from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import portalocker

IS_WINDOWS = os.name == "nt"


class ConcurrentWriteError(RuntimeError):
    pass


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)


def secure_file(path: Path) -> None:
    if not IS_WINDOWS:
        os.chmod(path, 0o600)


def private_mode_ok(path: Path, expected: int) -> bool:
    if IS_WINDOWS:
        return True
    return stat.S_IMODE(path.stat().st_mode) == expected


def atomic_json(path: Path, payload: Any) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_bytes(path, content.encode("utf-8"))


def atomic_bytes(
    path: Path, payload: bytes, *, expected_digest: str | None = None
) -> None:
    secure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if not IS_WINDOWS:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_digest is not None:
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = b""
            if hashlib.sha256(current).hexdigest() != expected_digest:
                raise ConcurrentWriteError("target changed during atomic write")
        _replace_with_retry(temporary_path, path)
        _sync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
        raise


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    secure_directory(path.parent)
    with path.open("a+b") as handle:
        secure_file(path)
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            yield
        finally:
            portalocker.unlock(handle)


def _replace_with_retry(source: Path, destination: Path) -> None:
    delays = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8) if IS_WINDOWS else (0.0,)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if index == len(delays) - 1:
                raise


def _sync_directory(path: Path) -> None:
    if IS_WINDOWS or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
