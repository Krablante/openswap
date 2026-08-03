from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .storage import ConcurrentWriteError, atomic_bytes

MAX_AUTH_BYTES = 1024 * 1024
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_SSH = re.compile(r"^[A-Za-z0-9_.@:-]+$")


class SyncError(Exception):
    def __init__(self, message: str, *, status: str = "error") -> None:
        super().__init__(message)
        self.status = status


class Workspace(str, Enum):
    OPENCODE = "opencode"
    CODEX = "codex"

    @classmethod
    def parse(cls, value: str | Workspace) -> Workspace:
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unsupported workspace: {value}") from exc

    def target_key(self, host: str) -> str:
        return host if self is Workspace.OPENCODE else f"{host}.codex"


@dataclass(frozen=True)
class SyncTarget:
    host: str
    workspace: Workspace
    path: Path | str
    ssh: str | None = None
    python: str = "python3"

    @property
    def key(self) -> str:
        """Stable schema-3 key used by persisted routing and sync state."""
        return self.workspace.target_key(self.host)


@dataclass(frozen=True)
class SyncConfig:
    interval_seconds: int
    connect_timeout_seconds: int
    command_timeout_seconds: int
    targets: tuple[SyncTarget, ...]


@dataclass(frozen=True)
class TargetSnapshot:
    target: SyncTarget
    document: dict[str, Any]
    digest: str


def read_target(config: SyncConfig, target: SyncTarget) -> TargetSnapshot:
    for attempt in range(2):
        raw = _read_target_bytes(config, target)
        if len(raw) > MAX_AUTH_BYTES:
            raise SyncError(f"auth document on {target.key} exceeds 1 MiB")
        if not raw:
            document = {}
            break
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            if attempt == 0:
                continue
            raise SyncError(
                f"auth document on {target.key} is temporarily malformed",
                status="pending",
            ) from exc
        if not isinstance(document, dict):
            raise SyncError(f"auth document on {target.key} must be an object")
        break
    return TargetSnapshot(
        target=target,
        document=document,
        digest=hashlib.sha256(raw).hexdigest(),
    )


def write_target(
    config: SyncConfig,
    snapshot: TargetSnapshot,
    document: dict[str, Any],
) -> str:
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    if len(payload) > MAX_AUTH_BYTES:
        raise SyncError(
            f"merged auth document for {snapshot.target.key} exceeds 1 MiB"
        )
    if snapshot.target.ssh is None:
        _write_local(snapshot.target.path, snapshot.digest, payload)
    else:
        _write_remote(config, snapshot.target, snapshot.digest, payload)
    return hashlib.sha256(payload).hexdigest()


def _read_target_bytes(config: SyncConfig, target: SyncTarget) -> bytes:
    if target.ssh is None:
        try:
            return target.path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError as exc:
            raise SyncError(f"cannot read local target {target.key}: {exc}") from exc
    script = (
        "import pathlib,sys\n"
        f"path = pathlib.Path({str(target.path)!r}).expanduser()\n"
        "sys.stdout.buffer.write(path.read_bytes() if path.exists() else b'')\n"
    )
    return _ssh_python(config, target, script).stdout


def _write_local(path: Path, expected: str, payload: bytes) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = b""
    except OSError as exc:
        raise SyncError(f"cannot read local auth target: {exc}") from exc
    if hashlib.sha256(current).hexdigest() != expected:
        raise SyncError(
            "local auth target changed during synchronization", status="pending"
        )
    try:
        atomic_bytes(path, payload, expected_digest=expected)
    except ConcurrentWriteError as exc:
        raise SyncError(
            "local auth target changed during synchronization", status="pending"
        ) from exc
    except OSError as exc:
        raise SyncError(f"cannot write local auth target: {exc}") from exc


def _write_remote(
    config: SyncConfig,
    target: SyncTarget,
    expected: str,
    payload: bytes,
) -> None:
    encoded = base64.b64encode(payload).decode("ascii")
    script = f'''
import base64, hashlib, json, os, pathlib, sys, tempfile, time
path = pathlib.Path({str(target.path)!r}).expanduser()
expected = {expected!r}
payload = base64.b64decode({encoded!r})
json.loads(payload)
current = path.read_bytes() if path.exists() else b""
if hashlib.sha256(current).hexdigest() != expected:
    raise SystemExit(75)
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{{path.name}}.openswap-", dir=path.parent)
try:
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    latest = path.read_bytes() if path.exists() else b""
    if hashlib.sha256(latest).hexdigest() != expected:
        raise SystemExit(75)
    delays = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8) if os.name == "nt" else (0.0,)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if index == len(delays) - 1:
                raise
    if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
'''
    _ssh_python(config, target, script)


def _ssh_python(
    config: SyncConfig,
    target: SyncTarget,
    script: str,
) -> subprocess.CompletedProcess[bytes]:
    assert target.ssh is not None
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.connect_timeout_seconds}",
        "-o",
        "ConnectionAttempts=1",
        "--",
        target.ssh,
        target.python,
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=script.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=config.command_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(
            f"SSH target {target.key} is unavailable: {exc}", status="offline"
        ) from exc
    if result.returncode == 75:
        raise SyncError(
            f"auth document on {target.key} changed during synchronization",
            status="pending",
        )
    if result.returncode != 0:
        reason = "unavailable" if result.returncode == 255 else "failed"
        raise SyncError(
            f"SSH target {target.key} {reason}",
            status="offline" if result.returncode == 255 else "error",
        )
    return result
