from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import Settings
from .storage import secure_file
from .sync import SAFE_NAME, SAFE_SSH, SyncConfig, SyncTarget
from .telegram import TelegramSettings


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    path: Path
    core: Settings
    telegram: TelegramSettings
    scheduler_interval_seconds: int


def load_config(path: Path | None = None) -> AppConfig:
    config_path = (path or Path.cwd() / "config.toml").resolve()
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"configuration file not found: {config_path}\n"
            "Copy config.example.toml to config.toml and edit it."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    try:
        secure_file(config_path)
    except OSError as exc:
        raise ConfigError(f"cannot secure {config_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("config.toml must contain a TOML document")
    _reject_unknown(document, {"telegram", "storage", "opencode", "codex", "service", "sync", "hosts"}, "root")

    base = config_path.parent
    telegram = _table(document, "telegram")
    _reject_unknown(telegram, {"token", "allowed_users"}, "telegram")
    token = _string(telegram, "token")
    allowed_raw = telegram.get("allowed_users")
    if not isinstance(allowed_raw, list) or not allowed_raw or any(
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        for user_id in allowed_raw
    ):
        raise ConfigError("telegram.allowed_users must be a non-empty array of positive integers")

    storage = _table(document, "storage")
    _reject_unknown(storage, {"directory"}, "storage")
    state_dir = _path(base, _string(storage, "directory"))

    opencode = _table(document, "opencode")
    _reject_unknown(opencode, {"auth_file"}, "opencode")
    target_auth = _path(base, _string(opencode, "auth_file"))

    codex = _table(document, "codex")
    _reject_unknown(codex, {"binary", "auth_file"}, "codex")
    codex_binary = _command_or_path(base, _string(codex, "binary"))
    codex_auth_value = codex.get("auth_file")
    if codex_auth_value is not None and (
        not isinstance(codex_auth_value, str) or not codex_auth_value.strip()
    ):
        raise ConfigError("codex.auth_file must be a non-empty string")
    codex_auth = (
        _path(base, codex_auth_value.strip())
        if isinstance(codex_auth_value, str)
        else None
    )

    service = _optional_table(document, "service")
    _reject_unknown(service, {"scheduler_interval_seconds"}, "service")
    scheduler_interval = _integer(service, "scheduler_interval_seconds", 60, minimum=5)

    sync_table = _optional_table(document, "sync")
    _reject_unknown(
        sync_table,
        {"interval_seconds", "connect_timeout_seconds", "command_timeout_seconds"},
        "sync",
    )
    hosts_raw = document.get("hosts", [])
    if not isinstance(hosts_raw, list):
        raise ConfigError("hosts must be an array of tables")
    sync_config = (
        _sync_config(base, sync_table, hosts_raw, codex_auth)
        if hosts_raw
        else None
    )
    if sync_config is not None:
        local_targets = [
            target
            for target in sync_config.targets
            if target.kind == "opencode"
            and target.ssh is None
            and target.path == target_auth
        ]
        if len(local_targets) != 1:
            raise ConfigError(
                "hosts must contain exactly one local entry whose auth_file matches opencode.auth_file"
            )
        if codex_auth is not None:
            local_codex_targets = [
                target
                for target in sync_config.targets
                if target.kind == "codex"
                and target.ssh is None
                and target.path == codex_auth
            ]
            if len(local_codex_targets) != 1:
                raise ConfigError(
                    "hosts must contain exactly one local codex_auth_file matching codex.auth_file"
                )

    return AppConfig(
        path=config_path,
        core=Settings(
            state_dir=state_dir,
            target_auth=target_auth,
            codex_auth=codex_auth,
            codex_bin=codex_binary,
            sync=sync_config,
            usage_stale_seconds=scheduler_interval * 3,
        ),
        telegram=TelegramSettings(
            token=token,
            allowed_users=frozenset(allowed_raw),
        ),
        scheduler_interval_seconds=scheduler_interval,
    )


def _sync_config(
    base: Path,
    table: dict[str, Any],
    hosts: list[Any],
    codex_auth: Path | None,
) -> SyncConfig:
    targets: list[SyncTarget] = []
    seen: set[str] = set()
    for index, raw in enumerate(hosts):
        if not isinstance(raw, dict):
            raise ConfigError(f"hosts[{index}] must be a table")
        _reject_unknown(
            raw,
            {"name", "auth_file", "codex_auth_file", "ssh", "python"},
            f"hosts[{index}]",
        )
        name = _string(raw, "name")
        if not SAFE_NAME.fullmatch(name) or len(name) > 32 or name in seen:
            raise ConfigError(
                f"hosts[{index}].name must be unique, at most 32 characters, and use ASCII letters, digits, dot, underscore, or hyphen"
            )
        seen.add(name)
        ssh = raw.get("ssh")
        if ssh is not None and (not isinstance(ssh, str) or not SAFE_SSH.fullmatch(ssh)):
            raise ConfigError(f"hosts[{index}].ssh is invalid")
        python = raw.get("python", "python3")
        if not isinstance(python, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", python):
            raise ConfigError(f"hosts[{index}].python is invalid")
        targets.append(
            SyncTarget(
                name=name,
                path=(
                    _remote_path(_string(raw, "auth_file"), index)
                    if ssh is not None
                    else _path(base, _string(raw, "auth_file"))
                ),
                kind="opencode",
                label=name,
                ssh=ssh,
                python=python,
            )
        )
        codex_value = raw.get("codex_auth_file")
        if codex_value is not None:
            if codex_auth is None:
                raise ConfigError(
                    f"hosts[{index}].codex_auth_file requires codex.auth_file"
                )
            if not isinstance(codex_value, str) or not codex_value.strip():
                raise ConfigError(
                    f"hosts[{index}].codex_auth_file must be a non-empty string"
                )
            target_name = f"{name}.codex"
            if target_name in seen:
                raise ConfigError(
                    f"hosts[{index}].codex_auth_file creates duplicate target {target_name}"
                )
            seen.add(target_name)
            targets.append(
                SyncTarget(
                    name=target_name,
                    path=(
                        _remote_path(codex_value.strip(), index, "codex_auth_file")
                        if ssh is not None
                        else _path(base, codex_value.strip())
                    ),
                    kind="codex",
                    label=name,
                    ssh=ssh,
                    python=python,
                )
            )
    return SyncConfig(
        interval_seconds=_integer(table, "interval_seconds", 120, minimum=10),
        connect_timeout_seconds=_integer(table, "connect_timeout_seconds", 5, minimum=1),
        command_timeout_seconds=_integer(table, "command_timeout_seconds", 15, minimum=1),
        targets=tuple(targets),
    )


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] table")
    return value


def _optional_table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _string(table: dict[str, Any], name: str) -> str:
    value = table.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(table: dict[str, Any], name: str, default: int, *, minimum: int) -> int:
    value = table.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _path(base: Path, value: str) -> Path:
    expanded = Path(value).expanduser()
    return (base / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()


def _command_or_path(base: Path, value: str) -> Path:
    if any(separator in value for separator in ("/", "\\")) or value.startswith((".", "~")):
        return _path(base, value)
    return Path(value)


def _remote_path(value: str, index: int, field: str = "auth_file") -> str:
    if value.startswith(("/", "~/", "\\\\")) or re.match(
        r"^[A-Za-z]:[\\/]", value
    ):
        return value
    raise ConfigError(f"hosts[{index}].{field} must be an absolute remote path")


def _reject_unknown(table: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown {label} setting: {unknown[0]}")
