"""State, token conversion, refresh, and atomic OpenCode auth publication."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .codex import CodexClient, CodexError, CodexSnapshot
from .storage import atomic_json, exclusive_lock, private_mode_ok, secure_directory
from .sync import (
    SyncConfig,
    SyncError,
    SyncTarget,
    TargetSnapshot,
    read_target,
    write_target,
)

UTC = dt.UTC
TOKEN_USAGE_REFRESH_SECONDS = 30 * 60
TOKEN_USAGE_STALE_SECONDS = 2 * 60 * 60


class OpenSwapError(RuntimeError):
    pass


class DeadSessionError(OpenSwapError):
    pass


@dataclass(frozen=True)
class AuthImportResult:
    status: str
    account: dict[str, Any]


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    target_auth: Path
    codex_bin: Path
    sync: SyncConfig | None
    refresh_margin_seconds: int = 20 * 60
    usage_stale_seconds: int = 3 * 60
    token_usage_refresh_seconds: int = TOKEN_USAGE_REFRESH_SECONDS
    token_usage_stale_seconds: int = TOKEN_USAGE_STALE_SECONDS


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def sanitize_error(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:240] if text else error.__class__.__name__


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def decode_jwt(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise OpenSwapError("OAuth access token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        result = json.loads(payload)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenSwapError("cannot decode OAuth access token") from exc
    if not isinstance(result, dict):
        raise OpenSwapError("OAuth JWT payload is not an object")
    return result


def token_expiry_ms(access_token: str) -> int:
    value = decode_jwt(access_token).get("exp")
    if not isinstance(value, (int, float)):
        raise OpenSwapError("OAuth access token has no expiry")
    return int(value * 1000)


def token_account_id(access_token: str) -> str | None:
    payload = decode_jwt(access_token)
    direct = payload.get("chatgpt_account_id")
    if isinstance(direct, str) and direct:
        return direct
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = auth.get("chatgpt_account_id")
        if isinstance(value, str) and value:
            return value
    return None


def fingerprint(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def credential_fingerprint(entry: dict[str, Any]) -> str:
    access = entry.get("access")
    refresh = entry.get("refresh")
    if not isinstance(access, str) or not isinstance(refresh, str):
        return ""
    return fingerprint(access, refresh)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise OpenSwapError(f"file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenSwapError(f"cannot read JSON {path}: {exc}") from exc


def account_name(account: dict[str, Any]) -> str:
    value = account.get("name") or account.get("alias")
    return str(value) if value else "Session"


def token_usage_stats(
    account: dict[str, Any],
    *,
    today: dt.date | None = None,
    stale_after_seconds: int = TOKEN_USAGE_STALE_SECONDS,
) -> dict[str, Any]:
    usage = account.get("token_usage")
    usage = usage if isinstance(usage, dict) else {}
    raw_daily = usage.get("daily")
    raw_daily = raw_daily if isinstance(raw_daily, list) else []
    current_date = today or utc_now().date()
    seven_day_start = current_date - dt.timedelta(days=6)
    thirty_day_start = current_date - dt.timedelta(days=29)
    all_daily = 0
    seven_days = 0
    thirty_days = 0
    for item in raw_daily:
        if not isinstance(item, dict):
            continue
        date_value = item.get("date")
        tokens = item.get("tokens")
        if (
            not isinstance(date_value, str)
            or not isinstance(tokens, int)
            or isinstance(tokens, bool)
            or tokens < 0
        ):
            continue
        try:
            day = dt.date.fromisoformat(date_value)
        except ValueError:
            continue
        all_daily += tokens
        if day > current_date:
            continue
        if day >= thirty_day_start:
            thirty_days += tokens
        if day >= seven_day_start:
            seven_days += tokens

    lifetime = usage.get("lifetime_tokens")
    if (
        not isinstance(lifetime, int)
        or isinstance(lifetime, bool)
        or lifetime < 0
    ):
        lifetime = all_daily
    checked_at = parse_time(account.get("token_usage_checked_at"))
    if checked_at is not None and checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=dt.UTC)
    stale = checked_at is None or utc_now() - checked_at > dt.timedelta(
        seconds=stale_after_seconds
    )
    if account.get("token_usage_error"):
        stale = True
    available = bool(usage) and (
        isinstance(usage.get("lifetime_tokens"), int) or bool(raw_daily)
    )
    return {
        "available": available,
        "seven_days": seven_days,
        "thirty_days": thirty_days,
        "lifetime": lifetime,
        "checked_at": checked_at,
        "stale": stale,
        "error": account.get("token_usage_error"),
    }


def token_usage_overview(
    accounts: list[dict[str, Any]],
    *,
    stale_after_seconds: int = TOKEN_USAGE_STALE_SECONDS,
) -> dict[str, Any]:
    rows = [
        {
            "account": account,
            "stats": token_usage_stats(
                account, stale_after_seconds=stale_after_seconds
            ),
        }
        for account in accounts
    ]
    available = [row for row in rows if row["stats"]["available"]]
    checked = [
        row["stats"]["checked_at"]
        for row in available
        if row["stats"]["checked_at"] is not None
    ]
    return {
        "rows": rows,
        "seven_days": sum(row["stats"]["seven_days"] for row in available),
        "thirty_days": sum(
            row["stats"]["thirty_days"] for row in available
        ),
        "lifetime": sum(row["stats"]["lifetime"] for row in available),
        "available": len(available),
        "fresh": sum(not row["stats"]["stale"] for row in available),
        "total": len(rows),
        "oldest_checked_at": min(checked) if checked else None,
    }


def openai_entry(document: Any, source: Path | str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise OpenSwapError(f"auth document is not an object: {source}")
    entry = document.get("openai")
    if not isinstance(entry, dict) or entry.get("type") != "oauth":
        raise OpenSwapError(f"no OpenAI OAuth entry in {source}")
    required = ("access", "refresh", "expires", "accountId")
    missing = [name for name in required if not entry.get(name)]
    if missing:
        raise OpenSwapError(f"OpenAI OAuth entry misses: {', '.join(missing)}")
    if not isinstance(entry["access"], str) or not isinstance(entry["refresh"], str):
        raise OpenSwapError("OpenAI OAuth tokens are malformed")
    claim_account = token_account_id(entry["access"])
    if claim_account and claim_account != entry["accountId"]:
        raise OpenSwapError("OpenAI account ID does not match the access token")
    return entry


class OpenSwap:
    REGISTRY_VERSION = 2

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.codex = CodexClient(settings.codex_bin)
        self._last_sync_monotonic = 0.0
        self._sync_dirty = True
        self._wake_event = threading.Event()

    @property
    def registry_path(self) -> Path:
        return self.settings.state_dir / "registry.json"

    @property
    def lock_path(self) -> Path:
        return self.settings.state_dir / "openswap.lock"

    @property
    def accounts_dir(self) -> Path:
        return self.settings.state_dir / "accounts"

    def initialize(self) -> None:
        secure_directory(self.settings.state_dir)
        secure_directory(self.accounts_dir)
        if not self.registry_path.exists():
            atomic_json(self.registry_path, self._empty_registry())

    def locked(self) -> Any:
        self.initialize()
        return exclusive_lock(self.lock_path)

    def import_opencode(self, source: Path) -> AuthImportResult:
        source = source.expanduser().resolve()
        return self.import_auth_document(read_json(source))

    def import_auth_document(self, document: Any) -> AuthImportResult:
        codex_auth = self._canonical_uploaded_auth(document)
        account_uuid, codex_home = self.begin_pending_account()
        promoted = False
        try:
            atomic_json(codex_home / "auth.json", codex_auth)
            with self.locked():
                registry = self._load_registry()
                self._reconcile_locked(registry)
                try:
                    snapshot, _ = self._usage_slot(account_uuid)
                except (CodexError, OpenSwapError) as exc:
                    if self._is_dead_auth_error(exc):
                        raise DeadSessionError(
                            "ChatGPT session is dead or its refresh token is revoked"
                        ) from None
                    raise
                uploaded = self._read_slot_entry(account_uuid)
                existing = self._find_account_by_external_id(
                    registry, uploaded["accountId"]
                )
                if existing is None:
                    account = self._new_account(
                        account_uuid,
                        self._allocate_sequence(registry),
                        uploaded,
                        snapshot,
                    )
                    registry["accounts"][account_uuid] = account
                    self._save_registry(registry)
                    promoted = True
                    return AuthImportResult("added", dict(account))

                existing_uuid = existing["id"]
                existing_dead = existing.get("last_error") == "login required"
                existing_token: dict[str, Any] | None = None
                if not existing_dead:
                    try:
                        existing_snapshot, existing_changed = self._usage_slot(
                            existing_uuid
                        )
                        existing_token = self._read_slot_entry(existing_uuid)
                        existing["expires"] = existing_token["expires"]
                        existing["email"] = self._account_email(
                            existing_snapshot, existing_uuid
                        ) or existing.get("email")
                        self._update_limits(
                            registry, existing_uuid, existing_snapshot
                        )
                        if existing_changed:
                            existing["last_refresh"] = iso_now()
                            if self._account_in_use(registry, existing_uuid):
                                self._propagate_account_locked(
                                    registry, existing_uuid, existing_token
                                )
                    except (CodexError, OpenSwapError) as exc:
                        if not self._is_dead_auth_error(exc):
                            raise
                        existing_dead = True

                should_replace = existing_dead or (
                    existing_token is not None
                    and uploaded["expires"] > existing_token["expires"]
                )
                if not should_replace:
                    self._save_registry(registry)
                    return AuthImportResult("ignored", dict(existing))

                staged_auth = self._read_slot_auth(account_uuid)
                atomic_json(
                    self._codex_home(existing_uuid) / "auth.json", staged_auth
                )
                replacement = self._read_slot_entry(existing_uuid)
                existing["account_id_fingerprint"] = fingerprint(
                    replacement["accountId"]
                )[:16]
                self._update_account(
                    registry, existing_uuid, replacement, snapshot
                )
                if self._account_in_use(registry, existing_uuid):
                    self._propagate_account_locked(
                        registry, existing_uuid, replacement
                    )
                self._save_registry(registry)
                return AuthImportResult("replaced", dict(existing))
        finally:
            if not promoted:
                shutil.rmtree(self._account_dir(account_uuid), ignore_errors=True)

    def export_auth_document(
        self, selector: str, export_format: str
    ) -> dict[str, Any]:
        if export_format not in {"codex", "opencode"}:
            raise OpenSwapError("unsupported auth export format")
        self.refresh(selector, include_limits=False)
        with self.locked():
            registry = self._load_registry()
            account_uuid, _ = self._resolve_account(registry, selector)
            codex_auth = self._canonical_uploaded_auth(
                self._read_slot_auth(account_uuid)
            )
        if export_format == "codex":
            return codex_auth
        tokens = codex_auth["tokens"]
        return {
            "openai": {
                "type": "oauth",
                "refresh": tokens["refresh_token"],
                "access": tokens["access_token"],
                "expires": token_expiry_ms(tokens["access_token"]),
                "accountId": tokens["account_id"],
            }
        }

    def add_account(self, *, browser: bool = False) -> dict[str, Any]:
        account_uuid = str(uuid.uuid4())
        codex_home = self._codex_home(account_uuid)
        with self.locked():
            self._load_registry()
            self._prepare_codex_home(codex_home)
        try:
            self.codex.login(codex_home, browser=browser)
            snapshot = self._inspect_slot(
                account_uuid, refresh=True, include_limits=True
            )
            token = self._read_slot_entry(account_uuid)
            with self.locked():
                registry = self._load_registry()
                existing = self._find_account_by_external_id(
                    registry, token["accountId"]
                )
                if existing:
                    raise OpenSwapError(
                        f"this ChatGPT account is already stored as {account_name(existing)}"
                    )
                account = self._new_account(
                    account_uuid,
                    self._allocate_sequence(registry),
                    token,
                    snapshot,
                )
                registry["accounts"][account_uuid] = account
                self._save_registry(registry)
                return account
        except BaseException:
            shutil.rmtree(self._account_dir(account_uuid), ignore_errors=True)
            raise

    def begin_pending_account(self) -> tuple[str, Path]:
        account_uuid = str(uuid.uuid4())
        with self.locked():
            self._load_registry()
            self._prepare_codex_home(self._codex_home(account_uuid))
        return account_uuid, self._codex_home(account_uuid)

    def finalize_pending_account(self, account_uuid: str) -> dict[str, Any]:
        self._validate_account_uuid(account_uuid)
        with self.locked():
            registry = self._load_registry()
            if account_uuid in registry["accounts"]:
                raise OpenSwapError("pending account is already registered")
            snapshot = self._inspect_slot(
                account_uuid, refresh=True, include_limits=True
            )
            token = self._read_slot_entry(account_uuid)
            existing = self._find_account_by_external_id(
                registry, token["accountId"]
            )
            if existing:
                raise OpenSwapError(
                    f"this ChatGPT account is already stored as {account_name(existing)}"
                )
            account = self._new_account(
                account_uuid,
                self._allocate_sequence(registry),
                token,
                snapshot,
            )
            registry["accounts"][account_uuid] = account
            self._save_registry(registry)
            return dict(account)

    def cancel_pending_account(self, account_uuid: str) -> None:
        self._validate_account_uuid(account_uuid)
        with self.locked():
            registry = self._load_registry()
            if account_uuid in registry["accounts"]:
                return
            shutil.rmtree(self._account_dir(account_uuid), ignore_errors=True)

    def begin_account_login(self, selector: str) -> tuple[str, Path, dict[str, Any]]:
        with self.locked():
            registry = self._load_registry()
            account_uuid, _ = self._resolve_account(registry, selector)
            auth_path = self._codex_home(account_uuid) / "auth.json"
            previous_auth = read_json(auth_path)
        try:
            self.codex.logout(self._codex_home(account_uuid))
        except BaseException:
            atomic_json(auth_path, previous_auth)
            raise
        return account_uuid, self._codex_home(account_uuid), previous_auth

    def finalize_account_login(
        self, account_uuid: str, previous_auth: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_account_uuid(account_uuid)
        try:
            snapshot = self._inspect_slot(
                account_uuid, refresh=True, include_limits=True
            )
            token = self._read_slot_entry(account_uuid)
            with self.locked():
                registry = self._load_registry()
                account = registry["accounts"][account_uuid]
                if account.get("account_id_fingerprint") != fingerprint(
                    token["accountId"]
                )[:16]:
                    raise OpenSwapError(
                        "the authorized ChatGPT account does not match this Session"
                    )
                self._update_account(registry, account_uuid, token, snapshot)
                if self._account_in_use(registry, account_uuid):
                    self._propagate_account_locked(registry, account_uuid, token)
                self._save_registry(registry)
                return dict(account)
        except BaseException:
            atomic_json(self._codex_home(account_uuid) / "auth.json", previous_auth)
            raise

    def cancel_account_login(
        self, account_uuid: str, previous_auth: dict[str, Any]
    ) -> None:
        self._validate_account_uuid(account_uuid)
        atomic_json(self._codex_home(account_uuid) / "auth.json", previous_auth)

    def remove_account(self, selector: str) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            account_uuid, account = self._resolve_account(registry, selector)
            if registry.get("default_account") == account_uuid:
                raise OpenSwapError("cannot remove the default account; switch first")
            assigned_targets = sorted(
                name
                for name, assigned_uuid in registry["target_overrides"].items()
                if assigned_uuid == account_uuid
            )
            if assigned_targets:
                raise OpenSwapError(
                    "cannot remove an assigned account; change first: "
                    + ", ".join(assigned_targets)
                )
            del registry["accounts"][account_uuid]
            self._normalize_accounts(registry)
            self._save_registry(registry)
            shutil.rmtree(self._account_dir(account_uuid), ignore_errors=True)
            return account

    def accounts(self) -> tuple[list[dict[str, Any]], str | None]:
        with self.locked():
            registry = self._load_registry()
            self._reconcile_locked(registry)
            self._save_registry(registry)
            target_counts = self._target_counts(registry)
            accounts = []
            for account_uuid, value in registry["accounts"].items():
                account = dict(value)
                account["target_count"] = target_counts.get(account_uuid, 0)
                accounts.append(account)
            accounts.sort(key=lambda value: int(value["sequence"]))
            return accounts, registry.get("default_account")

    def use(self, selector: str, *, all_targets: bool = False) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            self._reconcile_locked(registry)
            account_uuid, _ = self._resolve_account(registry, selector)
            snapshot = self._inspect_slot(
                account_uuid, refresh=True, include_limits=True
            )
            token = self._read_slot_entry(account_uuid)
            self._update_account(registry, account_uuid, token, snapshot)
            registry["accounts"][account_uuid]["limits_refresh_source"] = "manual"
            registry["default_account"] = account_uuid
            if all_targets:
                registry["target_overrides"] = {}
            else:
                registry["target_overrides"] = {
                    target_name: assigned_uuid
                    for target_name, assigned_uuid in registry["target_overrides"].items()
                    if assigned_uuid != account_uuid
                }
            self._publish_local_assignment_locked(registry)
            self._mark_routing_pending(registry)
            self._mark_sync_dirty()
            self._save_registry(registry)
            return dict(registry["accounts"][account_uuid])

    def assign_target(self, target_name: str, selector: str) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            config = self._sync_configuration()
            target = self._resolve_target(config, target_name)
            account_uuid, account = self._resolve_account(registry, selector)
            if account.get("last_error") == "login required":
                raise DeadSessionError("login required")
            if account_uuid == registry.get("default_account"):
                registry["target_overrides"].pop(target.name, None)
            else:
                registry["target_overrides"][target.name] = account_uuid
            self._mark_target_pending(registry, target.name)
            if self._is_local_target(target):
                self._publish_local_assignment_locked(registry)
            registry["updated_at"] = iso_now()
            self._mark_sync_dirty()
            self._save_registry(registry)
            return self._target_assignment(registry, target)

    def unassign_target(self, target_name: str) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            config = self._sync_configuration()
            target = self._resolve_target(config, target_name)
            registry["target_overrides"].pop(target.name, None)
            self._mark_target_pending(registry, target.name)
            if self._is_local_target(target):
                self._publish_local_assignment_locked(registry)
            registry["updated_at"] = iso_now()
            self._mark_sync_dirty()
            self._save_registry(registry)
            return self._target_assignment(registry, target)

    def clear_target_overrides(self) -> int:
        with self.locked():
            registry = self._load_registry()
            removed = len(registry["target_overrides"])
            registry["target_overrides"] = {}
            self._publish_local_assignment_locked(registry)
            self._mark_routing_pending(registry)
            registry["updated_at"] = iso_now()
            self._mark_sync_dirty()
            self._save_registry(registry)
            return removed

    def target_override_count(self) -> int:
        with self.locked():
            return len(self._load_registry()["target_overrides"])

    def refresh(self, selector: str, *, include_limits: bool = True) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            self._reconcile_locked(registry)
            account_uuid, _ = self._resolve_account(registry, selector)
            snapshot = self._inspect_slot(
                account_uuid,
                refresh=True,
                include_limits=include_limits,
            )
            token = self._read_slot_entry(account_uuid)
            self._update_account(registry, account_uuid, token, snapshot)
            if include_limits:
                registry["accounts"][account_uuid][
                    "limits_refresh_source"
                ] = "manual"
            if self._account_in_use(registry, account_uuid):
                self._propagate_account_locked(registry, account_uuid, token)
            self._save_registry(registry)
            return dict(registry["accounts"][account_uuid])

    def refresh_all(self) -> list[dict[str, Any]]:
        accounts, _ = self.accounts()
        refreshed = []
        for account in accounts:
            if account.get("last_error") == "login required":
                refreshed.append(account)
                continue
            refreshed.append(self.refresh(account["id"]))
        return refreshed

    def refresh_scheduler_data(
        self, *, max_age_seconds: int, source: str = "scheduler"
    ) -> dict[str, Any]:
        checked: list[str] = []
        refreshed: list[str] = []
        errors: list[str] = []
        now = utc_now()
        with self.locked():
            registry = self._load_registry()
            self._reconcile_locked(registry)
            for account_uuid, account in registry["accounts"].items():
                if account.get("last_error") == "login required":
                    continue
                name = account_name(account)
                try:
                    token = self._read_slot_entry(account_uuid)
                    expires_at = float(token["expires"]) / 1000
                    token_due = (
                        expires_at
                        <= now.timestamp() + self.settings.refresh_margin_seconds
                    )
                    if token_due:
                        snapshot = self._inspect_slot(
                            account_uuid, refresh=True, include_limits=False
                        )
                        token = self._read_slot_entry(account_uuid)
                        self._update_account(registry, account_uuid, token, snapshot)
                        if self._account_in_use(registry, account_uuid):
                            self._propagate_account_locked(
                                registry, account_uuid, token
                            )
                        refreshed.append(name)

                    checked_at = parse_time(account.get("limits_checked_at"))
                    usage_due = max_age_seconds <= 0 or checked_at is None or (
                        now - checked_at
                    ).total_seconds() >= max_age_seconds
                    if usage_due:
                        usage, token_changed = self._usage_slot(account_uuid)
                        self._update_limits(registry, account_uuid, usage)
                        if token_changed:
                            token = self._read_slot_entry(account_uuid)
                            account["expires"] = token["expires"]
                            account["last_refresh"] = iso_now()
                            if self._account_in_use(registry, account_uuid):
                                self._propagate_account_locked(
                                    registry, account_uuid, token
                                )
                        checked.append(name)
                        account["limits_refresh_source"] = source
                        account["usage_error"] = None
                        account["usage_error_at"] = None
                        account["usage_failures"] = 0
                except (OpenSwapError, CodexError, TypeError, ValueError) as exc:
                    message = sanitize_error(exc)
                    account["usage_error"] = message
                    account["usage_error_at"] = iso_now()
                    account["limits_refresh_source"] = source
                    account["usage_failures"] = int(
                        account.get("usage_failures") or 0
                    ) + 1
                    if self._authentication_error(exc):
                        account["last_error"] = "login required"
                    errors.append(f"{name}: {message}")
            self._save_registry(registry)
        return {"checked": checked, "refreshed": refreshed, "errors": errors}

    def refresh_all_usage(self) -> list[str]:
        return self.refresh_scheduler_data(
            max_age_seconds=0, source="manual"
        )["checked"]

    def refresh_token_usage(
        self,
        selector: str,
        *,
        force: bool = True,
        max_age_seconds: int | None = None,
    ) -> dict[str, Any]:
        max_age = (
            self.settings.token_usage_refresh_seconds
            if max_age_seconds is None
            else max_age_seconds
        )
        with self.locked():
            registry = self._load_registry()
            account_uuid, account = self._resolve_account(registry, selector)
            attempted_at = parse_time(
                account.get("token_usage_attempted_at")
                or account.get("token_usage_checked_at")
            )
            if (
                not force
                and attempted_at is not None
                and utc_now() - attempted_at < dt.timedelta(seconds=max_age)
            ):
                return dict(account)
            if account.get("last_error") == "login required":
                return dict(account)
            codex_home = self._codex_home(account_uuid)

        attempted = iso_now()
        usage: dict[str, Any] | None = None
        error: str | None = None
        try:
            usage = self.codex.account_usage(codex_home)
        except (CodexError, OpenSwapError, OSError) as exc:
            error = sanitize_error(exc)

        with self.locked():
            registry = self._load_registry()
            account = registry["accounts"].get(account_uuid)
            if account is None:
                raise OpenSwapError(
                    "account was removed during token usage refresh"
                )
            account["token_usage_attempted_at"] = attempted
            if usage is not None:
                account["token_usage"] = usage
                account["token_usage_checked_at"] = attempted
                account["token_usage_error"] = None
            else:
                account["token_usage_error"] = (
                    error or "token usage refresh failed"
                )
            self._save_registry(registry)
            return dict(account)

    def refresh_all_token_usage(
        self,
        *,
        force: bool = False,
        max_age_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.locked():
            registry = self._load_registry()
            account_ids = [
                account_uuid
                for account_uuid, account in registry["accounts"].items()
                if account.get("last_error") != "login required"
            ]
        for account_uuid in account_ids:
            self.refresh_token_usage(
                account_uuid,
                force=force,
                max_age_seconds=max_age_seconds,
            )
        return self.accounts()[0]

    def consume_reset(
        self, selector: str, *, idempotency_key: str
    ) -> tuple[dict[str, Any], str]:
        if not idempotency_key:
            raise OpenSwapError("reset idempotency key is empty")
        with self.locked():
            registry = self._load_registry()
            self._reconcile_locked(registry)
            account_uuid, _ = self._resolve_account(registry, selector)
            snapshot = self._inspect_slot(
                account_uuid,
                refresh=True,
                include_limits=True,
                consume_reset_key=idempotency_key,
            )
            outcome = snapshot.reset_outcome
            if outcome not in {"reset", "alreadyRedeemed", "nothingToReset", "noCredit"}:
                raise OpenSwapError("Codex returned an unknown reset outcome")
            token = self._read_slot_entry(account_uuid)
            self._update_account(registry, account_uuid, token, snapshot)
            if self._account_in_use(registry, account_uuid):
                self._propagate_account_locked(registry, account_uuid, token)
            self._save_registry(registry)
            return dict(registry["accounts"][account_uuid]), outcome

    def status(self) -> dict[str, Any]:
        with self.locked():
            registry = self._load_registry()
            drift = self._reconcile_locked(registry)
            self._save_registry(registry)
            default_account = registry.get("default_account")
            return {
                "accounts": len(registry["accounts"]),
                "default": (
                    account_name(registry["accounts"][default_account])
                    if default_account in registry["accounts"]
                    else None
                ),
                "drift": drift,
                "target": str(self.settings.target_auth),
                "state": str(self.settings.state_dir),
                "sync": self._sync_summary(registry),
            }

    def sync_targets(self, *, force: bool = True) -> dict[str, Any]:
        config = self._sync_configuration()
        if config is None:
            return {"enabled": False, "total": 0, "synced": 0, "targets": []}
        now_monotonic = time.monotonic()
        if (
            not force
            and not self._sync_dirty
            and now_monotonic - self._last_sync_monotonic < config.interval_seconds
        ):
            status = self.sync_status()
            if not any(
                target.get("status") == "pending" and not target.get("error")
                for target in status["targets"]
            ):
                return status

        with self._sync_locked():
            with self.locked():
                registry = self._load_registry()
                desired_uuids = {
                    target.name: self._desired_account_uuid(registry, target.name)
                    for target in config.targets
                }
            snapshots: dict[str, TargetSnapshot] = {}
            errors: dict[str, SyncError] = {}
            for target in config.targets:
                try:
                    snapshots[target.name] = read_target(config, target)
                except SyncError as exc:
                    errors[target.name] = exc

            adopted = False
            for target in config.targets:
                snapshot = snapshots.get(target.name)
                desired_uuid = desired_uuids.get(target.name)
                if snapshot is None or desired_uuid is None:
                    continue
                try:
                    adopted = (
                        self._adopt_sync_candidate(snapshot.document, desired_uuid)
                        or adopted
                    )
                except (CodexError, OpenSwapError):
                    continue

            if adopted:
                for target in config.targets:
                    if target.name in errors:
                        continue
                    try:
                        snapshots[target.name] = read_target(config, target)
                    except SyncError as exc:
                        snapshots.pop(target.name, None)
                        errors[target.name] = exc

            with self.locked():
                registry = self._load_registry()
                desired_uuids = {
                    target.name: self._desired_account_uuid(registry, target.name)
                    for target in config.targets
                }
                desired_entries: dict[str, dict[str, Any]] = {}
                for account_uuid in set(desired_uuids.values()):
                    if account_uuid in registry["accounts"]:
                        desired_entries[account_uuid] = self._read_slot_entry(
                            account_uuid
                        )

            checked_at = iso_now()
            target_status: dict[str, dict[str, Any]] = {}
            for target in config.targets:
                desired_uuid = desired_uuids.get(target.name)
                desired_account = registry["accounts"].get(desired_uuid)
                desired_entry = desired_entries.get(desired_uuid)
                desired_name = (
                    account_name(desired_account) if desired_account is not None else None
                )
                if target.name in errors:
                    error = errors[target.name]
                    target_status[target.name] = {
                        "name": target.name,
                        "status": error.status,
                        "session": desired_name,
                        "checked_at": checked_at,
                        "error": str(error),
                    }
                    continue
                snapshot = snapshots[target.name]
                if desired_entry is None:
                    target_status[target.name] = {
                        "name": target.name,
                        "status": "empty",
                        "session": None,
                        "checked_at": checked_at,
                        "error": None,
                    }
                    continue
                current = snapshot.document.get("openai")
                current_fingerprint = (
                    credential_fingerprint(current)
                    if isinstance(current, dict)
                    else None
                )
                expected_fingerprint = credential_fingerprint(desired_entry)
                try:
                    if current_fingerprint != expected_fingerprint:
                        write_target(config, snapshot, desired_entry)
                    target_status[target.name] = {
                        "name": target.name,
                        "status": "synced",
                        "session": desired_name,
                        "checked_at": checked_at,
                        "error": None,
                    }
                except SyncError as exc:
                    if exc.status == "pending":
                        try:
                            latest = read_target(config, target)
                            latest_openai = latest.document.get("openai")
                            if (
                                isinstance(latest_openai, dict)
                                and credential_fingerprint(latest_openai)
                                == expected_fingerprint
                            ):
                                target_status[target.name] = {
                                    "name": target.name,
                                    "status": "synced",
                                    "session": desired_name,
                                    "checked_at": checked_at,
                                    "error": None,
                                }
                                continue
                        except SyncError as latest_error:
                            exc = latest_error
                    target_status[target.name] = {
                        "name": target.name,
                        "status": exc.status,
                        "session": desired_name,
                        "checked_at": checked_at,
                        "error": str(exc),
                    }
                except OpenSwapError as exc:
                    target_status[target.name] = {
                        "name": target.name,
                        "status": "error",
                        "session": None,
                        "checked_at": checked_at,
                        "error": str(exc),
                    }

            with self.locked():
                registry = self._load_registry()
                latest_uuids = {
                    target.name: self._desired_account_uuid(registry, target.name)
                    for target in config.targets
                }
                latest_entries = {
                    account_uuid: self._read_slot_entry(account_uuid)
                    for account_uuid in set(latest_uuids.values())
                    if account_uuid in registry["accounts"]
                }
                changed_targets: set[str] = set()
                for target in config.targets:
                    latest_uuid = latest_uuids[target.name]
                    if latest_uuid != desired_uuids[target.name]:
                        changed_targets.add(target.name)
                        continue
                    previous_entry = desired_entries.get(latest_uuid)
                    if latest_uuid not in registry["accounts"] or previous_entry is None:
                        continue
                    latest_entry = latest_entries[latest_uuid]
                    if credential_fingerprint(previous_entry) != credential_fingerprint(
                        latest_entry
                    ):
                        changed_targets.add(target.name)
                for target_name in changed_targets:
                    status = target_status[target_name]
                    latest_uuid = latest_uuids[target_name]
                    latest_account = registry["accounts"].get(latest_uuid)
                    status["session"] = (
                        account_name(latest_account)
                        if latest_account is not None
                        else None
                    )
                    if status["status"] == "synced":
                        status["status"] = "pending"
                registry["sync"] = {
                    "checked_at": checked_at,
                    "targets": target_status,
                }
                for target in config.targets:
                    desired_uuid = latest_uuids[target.name]
                    desired_entry = desired_entries.get(desired_uuid)
                    if (
                        not self._is_local_target(target)
                        or target.name in changed_targets
                        or desired_entry is None
                        or target_status[target.name]["status"] != "synced"
                        or desired_uuid not in registry["accounts"]
                    ):
                        continue
                    registry["accounts"][desired_uuid][
                        "last_export_fingerprint"
                    ] = credential_fingerprint(desired_entry)
                    registry["accounts"][desired_uuid]["last_exported_at"] = checked_at
                self._save_registry(registry)

            self._last_sync_monotonic = time.monotonic()
            self._sync_dirty = bool(changed_targets)
            if changed_targets:
                self._wake_event.set()
            return self.sync_status()

    def sync_status(self) -> dict[str, Any]:
        config = self._sync_configuration()
        if config is None:
            return {"enabled": False, "total": 0, "synced": 0, "targets": []}
        with self.locked():
            registry = self._load_registry()
            return self._sync_summary(registry, config)

    def request_sync(self) -> None:
        self._mark_sync_dirty()

    def wait_for_work(self, timeout: float) -> None:
        self._wake_event.wait(timeout)
        self._wake_event.clear()

    def wake(self) -> None:
        self._wake_event.set()

    def system_status(self) -> dict[str, Any]:
        self.initialize()
        issues: list[str] = []
        storage_ok = private_mode_ok(self.settings.state_dir, 0o700)
        if not storage_ok:
            issues.append("storage_permissions")

        codex_version: str | None = None
        try:
            version_text = self.codex.version()
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
            codex_ok = bool(
                match and tuple(int(value) for value in match.groups()) >= (0, 146, 0)
            )
            codex_version = version_text
        except CodexError:
            codex_ok = False
        if not codex_ok:
            issues.append("codex_unavailable")

        opencode_status = "ready"
        try:
            document = read_json(self.settings.target_auth)
            openai_entry(document, self.settings.target_auth)
            if not private_mode_ok(self.settings.target_auth, 0o600):
                opencode_status = "unsafe_permissions"
                issues.append("opencode_permissions")
        except OpenSwapError:
            opencode_status = (
                "missing" if not self.settings.target_auth.exists() else "invalid"
            )
            issues.append(f"opencode_{opencode_status}")

        session_total = 0
        session_healthy = 0
        usage_fresh = 0
        token_usage_fresh = 0
        try:
            with self.locked():
                registry = self._load_registry()
                session_total = len(registry["accounts"])
                session_healthy = sum(
                    account.get("last_error") != "login required"
                    for account in registry["accounts"].values()
                )
                now = utc_now()
                for account_uuid, account in registry["accounts"].items():
                    self._read_slot_entry(account_uuid)
                    if account.get("last_error") == "login required":
                        continue
                    checked_at = parse_time(account.get("limits_checked_at"))
                    if (
                        not account.get("usage_error")
                        and checked_at is not None
                        and (now - checked_at).total_seconds()
                        <= self.settings.usage_stale_seconds
                    ):
                        usage_fresh += 1
                    token_stats = token_usage_stats(
                        account,
                        stale_after_seconds=(
                            self.settings.token_usage_stale_seconds
                        ),
                    )
                    if token_stats["available"] and not token_stats["stale"]:
                        token_usage_fresh += 1
        except OpenSwapError:
            issues.append("storage_invalid")
            storage_ok = False
        if session_healthy != session_total:
            issues.append("sessions_require_login")
        if usage_fresh != session_healthy:
            issues.append("usage_stale")
        if token_usage_fresh != session_healthy:
            issues.append("token_usage_stale")

        sync = self.sync_status()
        if sync["enabled"] and sync["synced"] != sync["total"]:
            issues.append("sync_incomplete")
        if self.settings.sync is not None and any(
            target.ssh is not None for target in self.settings.sync.targets
        ) and shutil.which("ssh") is None:
            issues.append("ssh_unavailable")

        return {
            "version": __version__,
            "ok": not issues,
            "issues": issues,
            "storage": {"ok": storage_ok},
            "codex": {"ok": codex_ok, "version": codex_version},
            "opencode": {"status": opencode_status},
            "sessions": {
                "healthy": session_healthy,
                "total": session_total,
                "usage_fresh": usage_fresh,
                "token_usage_fresh": token_usage_fresh,
            },
            "sync": sync,
        }

    def set_telegram_offset(self, value: int) -> None:
        with self.locked():
            registry = self._load_registry()
            registry["telegram_offset"] = value
            self._save_registry(registry)

    def telegram_offset(self) -> int:
        with self.locked():
            return int(self._load_registry().get("telegram_offset", 0))

    def telegram_menus(self) -> dict[str, dict[str, Any]]:
        with self.locked():
            menus = self._load_registry().get("telegram_menus", {})
            return dict(menus) if isinstance(menus, dict) else {}

    def telegram_language(self, user_id: int) -> str:
        with self.locked():
            languages = self._load_registry().get("telegram_languages", {})
            if not isinstance(languages, dict):
                return "en"
            return "ru" if languages.get(str(user_id)) == "ru" else "en"

    def set_telegram_language(self, user_id: int, language: str) -> None:
        if language not in {"en", "ru"}:
            raise OpenSwapError(f"unsupported Telegram language: {language}")
        with self.locked():
            registry = self._load_registry()
            languages = registry.setdefault("telegram_languages", {})
            if language == "en":
                languages.pop(str(user_id), None)
            else:
                languages[str(user_id)] = language
            self._save_registry(registry)

    def set_telegram_menu(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        view_account: str | None,
    ) -> None:
        with self.locked():
            registry = self._load_registry()
            menus = registry.setdefault("telegram_menus", {})
            menus[str(chat_id)] = {
                "user_id": user_id,
                "message_id": message_id,
                "view_account": view_account,
            }
            self._save_registry(registry)

    def clear_telegram_menu(self, chat_id: int) -> None:
        with self.locked():
            registry = self._load_registry()
            registry.setdefault("telegram_menus", {}).pop(str(chat_id), None)
            self._save_registry(registry)

    def _sync_locked(self) -> Any:
        self.initialize()
        return exclusive_lock(self.settings.state_dir / "sync.lock")

    def _sync_configuration(self) -> SyncConfig | None:
        return self.settings.sync

    @staticmethod
    def _resolve_target(config: SyncConfig | None, target_name: str) -> SyncTarget:
        if config is None:
            raise OpenSwapError("host synchronization is not configured")
        matches = [target for target in config.targets if target.name == target_name]
        if len(matches) != 1:
            raise OpenSwapError(f"unknown target: {target_name}")
        return matches[0]

    def _is_local_target(self, target: SyncTarget) -> bool:
        return target.ssh is None and target.path == self.settings.target_auth

    @staticmethod
    def _desired_account_uuid(
        registry: dict[str, Any], target_name: str
    ) -> str | None:
        override = registry["target_overrides"].get(target_name)
        return override if isinstance(override, str) else registry.get("default_account")

    def _target_assignment(
        self, registry: dict[str, Any], target: SyncTarget
    ) -> dict[str, Any]:
        account_uuid = self._desired_account_uuid(registry, target.name)
        account = registry["accounts"].get(account_uuid)
        return {
            "name": target.name,
            "account_id": account_uuid,
            "session": account_name(account) if account is not None else None,
            "override": target.name in registry["target_overrides"],
        }

    def _target_counts(self, registry: dict[str, Any]) -> dict[str, int]:
        config = self._sync_configuration()
        counts: dict[str, int] = {}
        if config is None:
            return counts
        for target in config.targets:
            account_uuid = self._desired_account_uuid(registry, target.name)
            if account_uuid is not None:
                counts[account_uuid] = counts.get(account_uuid, 0) + 1
        return counts

    @staticmethod
    def _account_in_use(registry: dict[str, Any], account_uuid: str) -> bool:
        return account_uuid == registry.get("default_account") or account_uuid in set(
            registry["target_overrides"].values()
        )

    def _local_account_uuid(self, registry: dict[str, Any]) -> str | None:
        config = self._sync_configuration()
        if config is not None:
            for target in config.targets:
                if self._is_local_target(target):
                    return self._desired_account_uuid(registry, target.name)
        return registry.get("default_account")

    def _publish_local_assignment_locked(self, registry: dict[str, Any]) -> None:
        config = self._sync_configuration()
        local_target = (
            next(
                (target for target in config.targets if self._is_local_target(target)),
                None,
            )
            if config is not None
            else None
        )
        account_uuid = (
            self._desired_account_uuid(registry, local_target.name)
            if local_target is not None
            else registry.get("default_account")
        )
        if account_uuid not in registry["accounts"]:
            return
        self._publish_locked(registry, account_uuid, self._read_slot_entry(account_uuid))
        if local_target is not None:
            registry.setdefault("sync", {}).setdefault("targets", {})[
                local_target.name
            ] = {
                "name": local_target.name,
                "status": "synced",
                "session": account_name(registry["accounts"][account_uuid]),
                "checked_at": iso_now(),
                "error": None,
            }

    def _propagate_account_locked(
        self,
        registry: dict[str, Any],
        account_uuid: str,
        token: dict[str, Any],
    ) -> None:
        if self._local_account_uuid(registry) == account_uuid:
            self._publish_locked(registry, account_uuid, token)
        self._mark_sync_dirty()

    @staticmethod
    def _mark_target_pending(registry: dict[str, Any], target_name: str) -> None:
        targets = registry.setdefault("sync", {}).setdefault("targets", {})
        previous = targets.get(target_name)
        targets[target_name] = {
            "name": target_name,
            "status": "pending",
            "session": None,
            "checked_at": previous.get("checked_at") if isinstance(previous, dict) else None,
            "error": None,
        }

    def _mark_routing_pending(self, registry: dict[str, Any]) -> None:
        config = self._sync_configuration()
        if config is None:
            return
        for target in config.targets:
            if (
                target.name not in registry["target_overrides"]
                and not self._is_local_target(target)
            ):
                self._mark_target_pending(registry, target.name)

    def _sync_summary(
        self,
        registry: dict[str, Any],
        config: SyncConfig | None = None,
    ) -> dict[str, Any]:
        if config is None:
            config = self._sync_configuration()
        if config is None:
            return {
                "enabled": False,
                "total": 0,
                "synced": 0,
                "default_account": registry.get("default_account"),
                "default_session": None,
                "override_count": 0,
                "targets": [],
            }
        sync_state = registry.get("sync")
        stored_targets = (
            sync_state.get("targets")
            if isinstance(sync_state, dict)
            and isinstance(sync_state.get("targets"), dict)
            else {}
        )
        targets: list[dict[str, Any]] = []
        for target in config.targets:
            assignment = self._target_assignment(registry, target)
            stored = stored_targets.get(target.name)
            if isinstance(stored, dict):
                item = dict(stored)
                item["name"] = target.name
            else:
                item = {
                    "name": target.name,
                    "status": "pending",
                    "session": None,
                    "checked_at": None,
                    "error": None,
                }
            item["account_id"] = assignment["account_id"]
            item["session"] = assignment["session"]
            item["override"] = assignment["override"]
            targets.append(item)
        default_uuid = registry.get("default_account")
        default_account = registry["accounts"].get(default_uuid)
        return {
            "enabled": True,
            "total": len(targets),
            "synced": sum(item.get("status") == "synced" for item in targets),
            "default_account": default_uuid,
            "default_session": (
                account_name(default_account) if default_account is not None else None
            ),
            "override_count": sum(item["override"] for item in targets),
            "checked_at": (
                sync_state.get("checked_at") if isinstance(sync_state, dict) else None
            ),
            "targets": targets,
        }

    def _adopt_sync_candidate(
        self, document: dict[str, Any], expected_uuid: str
    ) -> bool:
        try:
            codex_document = self._canonical_uploaded_auth(document)
        except OpenSwapError:
            return False
        tokens = codex_document["tokens"]
        candidate = {
            "type": "oauth",
            "access": tokens["access_token"],
            "refresh": tokens["refresh_token"],
            "expires": token_expiry_ms(tokens["access_token"]),
            "accountId": tokens["account_id"],
        }
        with self.locked():
            registry = self._load_registry()
            existing = registry["accounts"].get(expected_uuid)
            if (
                existing is None
                or existing.get("account_id_fingerprint")
                != fingerprint(candidate["accountId"])[:16]
            ):
                return False
            current = self._read_slot_entry(expected_uuid)
            if credential_fingerprint(candidate) == credential_fingerprint(current):
                return False
            if (
                candidate["expires"] < current["expires"]
                and existing.get("last_error") != "login required"
            ):
                return False

        staging_uuid, _ = self.begin_pending_account()
        try:
            atomic_json(
                self._codex_home(staging_uuid) / "auth.json", codex_document
            )
            self._force_refresh_slot(staging_uuid)
            refreshed = self._read_slot_entry(staging_uuid)
            if refreshed["accountId"] != candidate["accountId"]:
                raise OpenSwapError("refreshed sync candidate changed account identity")
            staged_auth = self._read_slot_auth(staging_uuid)
            with self.locked():
                registry = self._load_registry()
                existing = registry["accounts"].get(expected_uuid)
                if (
                    existing is None
                    or existing.get("account_id_fingerprint")
                    != fingerprint(refreshed["accountId"])[:16]
                ):
                    return False
                atomic_json(
                    self._codex_home(expected_uuid) / "auth.json", staged_auth
                )
                replacement = self._read_slot_entry(expected_uuid)
                existing["account_id_fingerprint"] = fingerprint(
                    replacement["accountId"]
                )[:16]
                existing["expires"] = replacement["expires"]
                existing["last_refresh"] = iso_now()
                existing["last_error"] = None
                if self._account_in_use(registry, expected_uuid):
                    self._propagate_account_locked(
                        registry, expected_uuid, replacement
                    )
                self._save_registry(registry)
            try:
                snapshot, _ = self._usage_slot(expected_uuid)
            except (CodexError, OpenSwapError):
                return True
            with self.locked():
                registry = self._load_registry()
                existing = registry["accounts"].get(expected_uuid)
                if existing is None:
                    return True
                replacement = self._read_slot_entry(expected_uuid)
                self._update_account(registry, expected_uuid, replacement, snapshot)
                if self._account_in_use(registry, expected_uuid):
                    self._propagate_account_locked(
                        registry, expected_uuid, replacement
                    )
                self._save_registry(registry)
            return True
        finally:
            shutil.rmtree(self._account_dir(staging_uuid), ignore_errors=True)

    def _empty_registry(self) -> dict[str, Any]:
        return {
            "version": self.REGISTRY_VERSION,
            "default_account": None,
            "target_overrides": {},
            "accounts": {},
            "next_sequence": 1,
            "telegram_offset": 0,
            "telegram_menus": {},
            "telegram_languages": {},
            "sync": {},
        }

    def _load_registry(self) -> dict[str, Any]:
        document = read_json(self.registry_path)
        if not isinstance(document, dict):
            raise OpenSwapError("unsupported or malformed OpenSwap registry")
        if document.get("version") == 1:
            document["version"] = self.REGISTRY_VERSION
            document["default_account"] = document.pop("active", None)
            document["target_overrides"] = {}
        elif document.get("version") != self.REGISTRY_VERSION:
            raise OpenSwapError("unsupported or malformed OpenSwap registry")
        if not isinstance(document.get("accounts"), dict):
            raise OpenSwapError("OpenSwap registry has no account map")
        if not isinstance(document.get("target_overrides"), dict):
            raise OpenSwapError("OpenSwap registry has no target override map")
        languages = document.setdefault("telegram_languages", {})
        if not isinstance(languages, dict) or any(
            not isinstance(user_id, str) or language != "ru"
            for user_id, language in languages.items()
        ):
            raise OpenSwapError("OpenSwap registry has an invalid Telegram language map")
        default_uuid = document.get("default_account")
        if default_uuid is not None and (
            not isinstance(default_uuid, str) or default_uuid not in document["accounts"]
        ):
            raise OpenSwapError("OpenSwap registry has an invalid default account")
        for target_name, account_uuid in document["target_overrides"].items():
            if not isinstance(target_name, str) or not target_name:
                raise OpenSwapError("OpenSwap registry has an invalid target override")
            if (
                not isinstance(account_uuid, str)
                or account_uuid not in document["accounts"]
            ):
                raise OpenSwapError(
                    f"OpenSwap registry has an invalid assignment for {target_name}"
                )
        self._normalize_accounts(document)
        return document

    def _save_registry(self, registry: dict[str, Any]) -> None:
        atomic_json(self.registry_path, registry)

    def _account_dir(self, account_uuid: str) -> Path:
        return self.accounts_dir / account_uuid

    @staticmethod
    def _validate_account_uuid(account_uuid: str) -> None:
        try:
            parsed = uuid.UUID(account_uuid)
        except (ValueError, AttributeError) as exc:
            raise OpenSwapError("invalid account ID") from exc
        if str(parsed) != account_uuid:
            raise OpenSwapError("invalid account ID")

    def _codex_home(self, account_uuid: str) -> Path:
        return self._account_dir(account_uuid) / "codex-home"

    def _prepare_codex_home(self, codex_home: Path) -> None:
        codex_home.mkdir(parents=True, exist_ok=False, mode=0o700)
        secure_directory(codex_home)
        config = codex_home / "config.toml"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                'cli_auth_credentials_store = "file"\n\n'
                "[features]\n"
                "plugins = false\n"
                "apps = false\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _canonical_uploaded_auth(document: Any) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise OpenSwapError("uploaded auth.json is not a JSON object")
        if "openai" in document:
            entry = openai_entry(document, "uploaded auth.json")
            access = entry["access"]
            refresh = entry["refresh"]
            account_id = entry["accountId"]
            id_token = access
            last_refresh = "1970-01-01T00:00:00Z"
        else:
            tokens = document.get("tokens")
            if not isinstance(tokens, dict):
                raise OpenSwapError(
                    "uploaded auth.json contains no ChatGPT OAuth session"
                )
            access = tokens.get("access_token")
            refresh = tokens.get("refresh_token")
            id_token = tokens.get("id_token")
            if not isinstance(access, str) or not access:
                raise OpenSwapError("uploaded Codex auth has no access token")
            if not isinstance(refresh, str) or not refresh:
                raise OpenSwapError("uploaded Codex auth has no refresh token")
            if not isinstance(id_token, str) or not id_token:
                id_token = access
            account_id = tokens.get("account_id") or token_account_id(access)
            if not isinstance(account_id, str) or not account_id:
                raise OpenSwapError(
                    "uploaded Codex auth has no ChatGPT account ID"
                )
            last_refresh = document.get("last_refresh")
            if not isinstance(last_refresh, str) or not last_refresh:
                last_refresh = "1970-01-01T00:00:00Z"
        claim_account = token_account_id(access)
        if claim_account and claim_account != account_id:
            raise OpenSwapError(
                "uploaded access token belongs to a different ChatGPT account"
            )
        token_expiry_ms(access)
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token,
                "access_token": access,
                "refresh_token": refresh,
                "account_id": account_id,
            },
            "last_refresh": last_refresh,
        }

    @staticmethod
    def _is_dead_auth_error(error: BaseException) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "http 401",
                "session has ended",
                "log in again",
                "invalid_grant",
                "token_invalidated",
                "refresh token is invalid",
            )
        )

    def _read_slot_auth(self, account_uuid: str) -> dict[str, Any]:
        path = self._codex_home(account_uuid) / "auth.json"
        document = read_json(path)
        if not isinstance(document, dict) or not isinstance(document.get("tokens"), dict):
            raise OpenSwapError(f"malformed Codex auth in account slot {account_uuid}")
        return document

    def _read_slot_entry(self, account_uuid: str) -> dict[str, Any]:
        auth = self._read_slot_auth(account_uuid)
        tokens = auth["tokens"]
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise OpenSwapError(f"incomplete Codex tokens in account slot {account_uuid}")
        account_id = tokens.get("account_id") or token_account_id(access)
        if not isinstance(account_id, str) or not account_id:
            raise OpenSwapError(f"cannot determine ChatGPT account for slot {account_uuid}")
        claim_account = token_account_id(access)
        if claim_account and claim_account != account_id:
            raise OpenSwapError(f"account mismatch in slot {account_uuid}")
        return {
            "type": "oauth",
            "refresh": refresh,
            "access": access,
            "expires": token_expiry_ms(access),
            "accountId": account_id,
        }

    def _inspect_slot(
        self,
        account_uuid: str,
        *,
        refresh: bool,
        include_limits: bool,
        consume_reset_key: str | None = None,
    ) -> CodexSnapshot:
        codex_home = self._codex_home(account_uuid)
        try:
            snapshot = self.codex.inspect(
                codex_home,
                refresh=refresh,
                consume_reset_key=consume_reset_key,
            )
        except CodexError as exc:
            message = str(exc).lower()
            if "401" not in message and "token_invalidated" not in message:
                raise
            self._force_refresh_slot(account_uuid)
            snapshot = self.codex.inspect(
                codex_home,
                refresh=False,
                consume_reset_key=consume_reset_key,
            )
        if refresh:
            token = self._read_slot_entry(account_uuid)
            refresh_before = int(
                (utc_now().timestamp() + self.settings.refresh_margin_seconds) * 1000
            )
            if token["expires"] <= refresh_before:
                self._force_refresh_slot(account_uuid)
                snapshot = self.codex.inspect(
                    codex_home,
                    refresh=False,
                    consume_reset_key=consume_reset_key,
                )
        if include_limits:
            usage, _ = self._usage_slot(account_uuid)
            return CodexSnapshot(
                account=usage.account or snapshot.account,
                rate_limits=usage.rate_limits,
                rate_limits_by_id=usage.rate_limits_by_id,
                reset_credits=usage.reset_credits,
                reset_outcome=snapshot.reset_outcome,
                limit_status=usage.limit_status,
            )
        return snapshot

    def _usage_slot(self, account_uuid: str) -> tuple[CodexSnapshot, bool]:
        token = self._read_slot_entry(account_uuid)
        try:
            return self.codex.usage(token["access"], token["accountId"]), False
        except CodexError as exc:
            if "401" not in str(exc):
                raise
            self._force_refresh_slot(account_uuid)
            token = self._read_slot_entry(account_uuid)
            return self.codex.usage(token["access"], token["accountId"]), True

    def _force_refresh_slot(self, account_uuid: str) -> None:
        auth = self._read_slot_auth(account_uuid)
        tokens = auth["tokens"]
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise OpenSwapError(f"slot {account_uuid} has no refresh token")
        updated = self.codex.refresh_tokens(refresh)
        access = updated["access_token"]
        tokens["access_token"] = access
        tokens["refresh_token"] = updated.get("refresh_token", refresh)
        tokens["id_token"] = updated.get("id_token", access)
        account_id = token_account_id(access)
        if account_id:
            tokens["account_id"] = account_id
        auth["last_refresh"] = iso_now()
        atomic_json(self._codex_home(account_uuid) / "auth.json", auth)

    def _new_account(
        self,
        account_uuid: str,
        sequence: int,
        token: dict[str, Any],
        snapshot: CodexSnapshot,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = iso_now()
        return {
            "id": account_uuid,
            "sequence": sequence,
            "alias": f"session-{sequence}",
            "name": f"Session {sequence}",
            "account_id_fingerprint": fingerprint(token["accountId"])[:16],
            "created_at": now,
            "last_refresh": None if error else now,
            "expires": token["expires"],
            "email": self._account_email(snapshot, account_uuid),
            "plan": self._plan(snapshot),
            "limits": snapshot.rate_limits,
            "limits_by_id": snapshot.rate_limits_by_id,
            "limit_status": snapshot.limit_status,
            "reset_credits": snapshot.reset_credits,
            "last_error": error,
            "last_export_fingerprint": None,
            "token_usage": None,
            "token_usage_checked_at": None,
            "token_usage_attempted_at": None,
            "token_usage_error": None,
        }

    def _update_account(
        self,
        registry: dict[str, Any],
        account_uuid: str,
        token: dict[str, Any],
        snapshot: CodexSnapshot,
    ) -> None:
        account = registry["accounts"][account_uuid]
        account["last_refresh"] = iso_now()
        account["expires"] = token["expires"]
        account["email"] = self._account_email(snapshot, account_uuid) or account.get(
            "email"
        )
        account["plan"] = self._plan(snapshot) or account.get("plan")
        self._update_limits(registry, account_uuid, snapshot)
        account["last_error"] = None

    def _update_limits(
        self,
        registry: dict[str, Any],
        account_uuid: str,
        snapshot: CodexSnapshot,
    ) -> None:
        account = registry["accounts"][account_uuid]
        account["plan"] = self._plan(snapshot) or account.get("plan")
        account["limits"] = snapshot.rate_limits
        account["limits_by_id"] = snapshot.rate_limits_by_id or {}
        account["limit_status"] = snapshot.limit_status
        if snapshot.reset_credits is not None:
            account["reset_credits"] = snapshot.reset_credits
        if any(
            value is not None
            for value in (
                snapshot.rate_limits,
                snapshot.rate_limits_by_id,
                snapshot.reset_credits,
                snapshot.limit_status,
            )
        ):
            account["limits_checked_at"] = iso_now()

    @staticmethod
    def _plan(snapshot: CodexSnapshot) -> str | None:
        if not isinstance(snapshot.account, dict):
            return None
        value = snapshot.account.get("planType")
        return value if isinstance(value, str) else None

    @staticmethod
    def _authentication_error(error: BaseException) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "401",
                "unauthorized",
                "token_invalidated",
                "refresh token",
            )
        )

    def _account_email(
        self, snapshot: CodexSnapshot, account_uuid: str
    ) -> str | None:
        if isinstance(snapshot.account, dict):
            value = snapshot.account.get("email")
            if isinstance(value, str) and "@" in value:
                return value
        try:
            auth = self._read_slot_auth(account_uuid)
        except OpenSwapError:
            return None
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            return None
        for key in ("id_token", "access_token"):
            token = tokens.get(key)
            if not isinstance(token, str):
                continue
            try:
                payload = decode_jwt(token)
            except OpenSwapError:
                continue
            value = payload.get("email")
            if isinstance(value, str) and "@" in value:
                return value
        return None

    @staticmethod
    def _normalize_accounts(registry: dict[str, Any]) -> None:
        ordered = sorted(
            registry["accounts"].items(),
            key=lambda item: (str(item[1].get("created_at", "")), item[0]),
        )
        for sequence, (_, account) in enumerate(ordered, start=1):
            account["sequence"] = sequence
            account["alias"] = f"session-{sequence}"
            account["name"] = f"Session {sequence}"
        registry["next_sequence"] = len(ordered) + 1

    @staticmethod
    def _allocate_sequence(registry: dict[str, Any]) -> int:
        sequence = registry.get("next_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            sequence = 1
        registry["next_sequence"] = sequence + 1
        return sequence

    def _resolve_account(
        self, registry: dict[str, Any], selector: str
    ) -> tuple[str, dict[str, Any]]:
        if selector in registry["accounts"]:
            return selector, registry["accounts"][selector]
        folded = selector.casefold().replace(" ", "-")
        matches = [
            (account_uuid, account)
            for account_uuid, account in registry["accounts"].items()
            if account["alias"].casefold() == folded
            or account_name(account).casefold().replace(" ", "-") == folded
        ]
        if len(matches) != 1:
            raise OpenSwapError(f"unknown account: {selector}")
        return matches[0]

    def _find_account_by_external_id(
        self, registry: dict[str, Any], account_id: str
    ) -> dict[str, Any] | None:
        expected = fingerprint(account_id)[:16]
        for account in registry["accounts"].values():
            if account.get("account_id_fingerprint") == expected:
                return account
        return None

    def _publish_locked(
        self,
        registry: dict[str, Any],
        account_uuid: str,
        token: dict[str, Any],
    ) -> None:
        if token["expires"] <= int((utc_now().timestamp() + 60) * 1000):
            raise OpenSwapError("refusing to publish an expired access token")
        document = (
            read_json(self.settings.target_auth)
            if self.settings.target_auth.exists()
            else {}
        )
        if not isinstance(document, dict):
            raise OpenSwapError("target auth document is not an object")
        document["openai"] = token
        atomic_json(self.settings.target_auth, document)
        published = openai_entry(read_json(self.settings.target_auth), self.settings.target_auth)
        if published["accountId"] != token["accountId"]:
            raise OpenSwapError("target auth verification failed after publication")
        registry["accounts"][account_uuid]["last_export_fingerprint"] = fingerprint(
            token["access"], token["refresh"]
        )
        registry["accounts"][account_uuid]["last_exported_at"] = iso_now()
        self._mark_sync_dirty()

    def _mark_sync_dirty(self) -> None:
        self._sync_dirty = True
        self._wake_event.set()

    def _reconcile_locked(self, registry: dict[str, Any]) -> str | None:
        if not self.settings.target_auth.exists():
            return "target auth is missing"
        target = openai_entry(read_json(self.settings.target_auth), self.settings.target_auth)
        matched_uuid = None
        expected = fingerprint(target["accountId"])[:16]
        for account_uuid, account in registry["accounts"].items():
            if account.get("account_id_fingerprint") == expected:
                matched_uuid = account_uuid
                break
        desired_uuid = self._local_account_uuid(registry)
        if matched_uuid is None:
            if desired_uuid in registry["accounts"]:
                slot = self._read_slot_entry(desired_uuid)
                self._publish_locked(registry, desired_uuid, slot)
                return "restored the assigned local account"
            return "target account is not imported"
        if desired_uuid not in registry["accounts"]:
            registry["default_account"] = matched_uuid
            desired_uuid = matched_uuid
        if matched_uuid != desired_uuid:
            slot = self._read_slot_entry(desired_uuid)
            self._publish_locked(registry, desired_uuid, slot)
            return "restored the assigned local account"
        account = registry["accounts"][matched_uuid]
        current_fp = fingerprint(target["access"], target["refresh"])
        if account.get("last_export_fingerprint") == current_fp:
            return None
        slot = self._read_slot_entry(matched_uuid)
        slot_fp = fingerprint(slot["access"], slot["refresh"])
        if current_fp == slot_fp:
            account["last_export_fingerprint"] = current_fp
            return None
        if target["expires"] >= slot["expires"]:
            if self.settings.sync is not None:
                return "newer target credentials are awaiting synchronization"
            auth = self._read_slot_auth(matched_uuid)
            auth["tokens"]["access_token"] = target["access"]
            auth["tokens"]["refresh_token"] = target["refresh"]
            auth["tokens"]["account_id"] = target["accountId"]
            auth["last_refresh"] = iso_now()
            atomic_json(self._codex_home(matched_uuid) / "auth.json", auth)
            account["expires"] = target["expires"]
            account["last_reconciled_at"] = iso_now()
            account["last_export_fingerprint"] = current_fp
            return None
        self._publish_locked(registry, matched_uuid, slot)
        return "restored newer central credentials"
