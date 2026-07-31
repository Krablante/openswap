"""Single-message Telegram interface for OpenSwap."""

from __future__ import annotations

import datetime as dt
import html
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .codex import CodexError, CodexLoginSession
from .core import (
    DeadSessionError,
    OpenSwap,
    OpenSwapError,
    account_name,
    parse_time,
    token_usage_overview,
    token_usage_stats,
)


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    allowed_users: frozenset[int]


@dataclass(frozen=True)
class PendingReset:
    account_id: str
    idempotency_key: str
    expires_monotonic: float


@dataclass(frozen=True)
class PendingDelete:
    account_id: str
    expires_monotonic: float


@dataclass(frozen=True)
class PendingLogin:
    account_id: str
    mode: str
    session: CodexLoginSession
    expires_monotonic: float
    previous_auth: dict[str, Any] | None = None


def _pick(language: str, english: str, russian: str) -> str:
    return russian if language == "ru" else english


def _status_icon(ready: bool) -> str:
    return "✓" if ready else "!"


def _host_count_label(count: int, language: str) -> str:
    if language != "ru":
        return f"{count} {'host' if count == 1 else 'hosts'}"
    remainder_100 = count % 100
    remainder_10 = count % 10
    if 11 <= remainder_100 <= 14:
        noun = "хостов"
    elif remainder_10 == 1:
        noun = "хост"
    elif 2 <= remainder_10 <= 4:
        noun = "хоста"
    else:
        noun = "хостов"
    return f"{count} {noun}"


def _pending_label(language: str, error: Any, *, icon: bool = False) -> str:
    prefix = "↻ " if icon else ""
    if error:
        return prefix + _pick(
            language, "busy · retry scheduled", "занят · повтор запланирован"
        )
    return prefix + _pick(language, "applying", "применяется")


def _system_issue_label(language: str, issue: str) -> str:
    labels = {
        "storage_permissions": (
            "Storage permissions are unsafe",
            "Небезопасные права хранилища",
        ),
        "storage_invalid": (
            "Session storage is invalid",
            "Хранилище сессий повреждено",
        ),
        "codex_unavailable": (
            "Codex is unavailable or unsupported",
            "Codex недоступен или не поддерживается",
        ),
        "opencode_missing": (
            "OpenCode auth.json is missing",
            "auth.json OpenCode не найден",
        ),
        "opencode_invalid": (
            "OpenCode auth.json is invalid",
            "auth.json OpenCode повреждён",
        ),
        "opencode_permissions": (
            "OpenCode auth.json permissions are unsafe",
            "Небезопасные права auth.json OpenCode",
        ),
        "sessions_require_login": (
            "One or more Sessions require login",
            "Одной или нескольким сессиям нужен вход",
        ),
        "usage_stale": (
            "Usage data is stale for one or more Sessions",
            "Данные лимитов одной или нескольких сессий устарели",
        ),
        "token_usage_stale": (
            "Token activity is stale for one or more Sessions",
            "Активность токенов одной или нескольких сессий устарела",
        ),
        "sync_incomplete": (
            "Some hosts are not synchronized",
            "Не все хосты синхронизированы",
        ),
        "ssh_unavailable": ("SSH is unavailable", "SSH недоступен"),
    }
    english, russian = labels.get(issue, (issue, issue))
    return _pick(language, english, russian)


def _duration_label(window: dict[str, Any], language: str) -> str:
    minutes = window.get("windowDurationMins")
    if not isinstance(minutes, (int, float)):
        return _pick(language, "Limit", "Лимит")
    minutes = round(minutes)
    if minutes % 1440 == 0:
        return _pick(language, f"{minutes // 1440}d", f"{minutes // 1440} дн.")
    if minutes % 60 == 0:
        return _pick(language, f"{minutes // 60}h", f"{minutes // 60} ч.")
    return _pick(language, f"{minutes}m", f"{minutes} мин.")


def _reset_time(timestamp: Any, language: str) -> str:
    if not isinstance(timestamp, (int, float)):
        return _pick(language, "time unknown", "время неизвестно")
    now = dt.datetime.now(dt.UTC)
    moment = dt.datetime.fromtimestamp(timestamp, dt.UTC)
    seconds = max(0, round((moment - now).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        countdown = _pick(language, f"{days}d {hours}h", f"{days}д {hours}ч")
    elif hours:
        countdown = _pick(language, f"{hours}h {minutes}m", f"{hours}ч {minutes}м")
    else:
        countdown = _pick(language, f"{minutes}m", f"{minutes}м")
    local = moment.astimezone()
    return _pick(
        language,
        f"in {countdown} · {local:%Y-%m-%d %H:%M}",
        f"через {countdown} · {local:%d.%m %H:%M}",
    )


def _reset_count(account: dict[str, Any]) -> int:
    reset_credits = account.get("reset_credits")
    if not isinstance(reset_credits, dict):
        return 0
    value = reset_credits.get("availableCount")
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _reset_short(timestamp: Any, language: str) -> str:
    if not isinstance(timestamp, (int, float)):
        return "—"
    seconds = max(
        0,
        round(
            (
                dt.datetime.fromtimestamp(timestamp, dt.UTC)
                - dt.datetime.now(dt.UTC)
            ).total_seconds()
        ),
    )
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return _pick(language, f"{days}d{hours}h", f"{days}д{hours}ч")
    if hours:
        return _pick(language, f"{hours}h{minutes}m", f"{hours}ч{minutes}м")
    return _pick(language, f"{minutes}m", f"{minutes}м")


def _rounded_token_value(value: int, divisor: int, precision: int) -> int:
    scale = 10**precision
    return (value * scale + divisor // 2) // divisor


def _token_unit(overview: dict[str, Any]) -> tuple[int, str, int]:
    largest = max(
        overview["seven_days"],
        overview["thirty_days"],
        overview["lifetime"],
    )
    if largest >= 1_000_000_000_000:
        return 1_000_000_000_000, "T", 3
    elif largest >= 1_000_000_000:
        return 1_000_000_000, "B", 3
    elif largest >= 1_000_000:
        return 1_000_000, "M", 3
    elif largest >= 1_000:
        return 1_000, "K", 3
    return 1, "", 0


def _format_rounded_token_value(
    rounded: int,
    unit: tuple[int, str, int],
    language: str,
    *,
    suffix: bool,
) -> str:
    _, label, precision = unit
    if precision == 0:
        formatted = f"{rounded:,}"
    else:
        scale = 10**precision
        whole, fraction = divmod(rounded, scale)
        formatted = f"{whole:,}.{fraction:0{precision}d}"
    if language == "ru":
        formatted = formatted.replace(",", "\u00a0").replace(".", ",")
    if not suffix:
        return formatted
    if language == "ru":
        label = {
            "K": " тыс.",
            "M": " млн",
            "B": " млрд",
            "T": " трлн",
        }.get(label, "")
    return formatted + label


def _format_token_value(
    value: int,
    unit: tuple[int, str, int],
    language: str,
    *,
    suffix: bool = True,
) -> str:
    divisor, _, precision = unit
    rounded = _rounded_token_value(value, divisor, precision)
    return _format_rounded_token_value(
        rounded, unit, language, suffix=suffix
    )


def _format_token_total(
    overview: dict[str, Any],
    field: str,
    unit: tuple[int, str, int],
    language: str,
    *,
    suffix: bool = True,
) -> str:
    divisor, _, precision = unit
    rounded = sum(
        _rounded_token_value(row["stats"][field], divisor, precision)
        for row in overview["rows"]
        if row["stats"]["available"]
    )
    return _format_rounded_token_value(
        rounded, unit, language, suffix=suffix
    )


def _token_unit_description(unit: tuple[int, str, int], language: str) -> str:
    label = unit[1]
    descriptions = {
        "K": ("thousands of tokens (K)", "тысячи токенов (тыс.)"),
        "M": ("millions of tokens (M)", "миллионы токенов (млн)"),
        "B": ("billions of tokens (B)", "миллиарды токенов (млрд)"),
        "T": ("trillions of tokens (T)", "триллионы токенов (трлн)"),
    }
    return _pick(language, *descriptions.get(label, ("tokens", "токены")))


def _token_age(moment: dt.datetime | None, language: str) -> str:
    if moment is None:
        return _pick(language, "never", "никогда")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    minutes = max(
        0,
        int((dt.datetime.now(dt.UTC) - moment).total_seconds() // 60),
    )
    if minutes < 1:
        return _pick(language, "just now", "только что")
    if minutes < 60:
        return _pick(language, f"{minutes}m ago", f"{minutes} мин. назад")
    hours = minutes // 60
    if hours < 24:
        return _pick(language, f"{hours}h ago", f"{hours} ч. назад")
    days = hours // 24
    return _pick(language, f"{days}d ago", f"{days} дн. назад")


def _token_usage_block(
    account: dict[str, Any],
    language: str,
    unit: tuple[int, str, int],
) -> list[str]:
    stats = token_usage_stats(account)
    if not stats["available"]:
        return [
            _pick(
                language,
                "🧮 Token activity: <i>not collected yet</i>",
                "🧮 Активность токенов: <i>ещё не собрана</i>",
            )
        ]
    stale = (
        _pick(language, " · stale", " · устарело")
        if stats["stale"]
        else ""
    )
    lines = [
        _pick(
            language,
            "🧮 <b>Token activity</b> · all Codex apps/devices",
            "🧮 <b>Активность токенов</b> · все приложения/устройства Codex",
        )
        + stale,
        _pick(language, "   7d ", "   7 д. ")
        + f"<b>{_format_token_value(stats['seven_days'], unit, language)}</b> · "
        + _pick(language, "30d ", "30 д. ")
        + f"<b>{_format_token_value(stats['thirty_days'], unit, language)}</b>",
        _pick(language, "   All time", "   За всё время")
        + f": <b>{_format_token_value(stats['lifetime'], unit, language)}</b>",
    ]
    if stats["stale"]:
        lines.append(
            _pick(language, "   Updated ", "   Обновлено ")
            + f"<i>{_token_age(stats['checked_at'], language)}</i>"
        )
    return lines


def _root_label(account: dict[str, Any], *, active: bool, language: str) -> str:
    marker = "●" if active else "○"
    name = account_name(account)[:20]
    if account.get("last_error") == "login required":
        return _pick(
            language,
            f"⚠ {name} · login required",
            f"⚠ {name} · нужен вход",
        )
    window = None
    for bucket in _limit_buckets(account):
        candidate = bucket.get("primary")
        if isinstance(candidate, dict):
            window = candidate
            break
    details = _limit_status_short(account, language)
    if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)):
        left = max(0, min(100, round(100 - window["usedPercent"])))
        details = f"{left}% · {_reset_short(window.get('resetsAt'), language)}"
    if account.get("usage_error"):
        details += _pick(language, " · stale", " · устарело")
    resets = _reset_count(account)
    reset_text = f" · ⚡{resets}" if resets else ""
    return f"{marker} {name} · {details}{reset_text}"


def _limit_buckets(account: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = account.get("limits_by_id")
    if isinstance(by_id, dict):
        buckets = [value for value in by_id.values() if isinstance(value, dict)]
        if buckets:
            return buckets
    limits = account.get("limits")
    return [limits] if isinstance(limits, dict) else []


def _limit_status(account: dict[str, Any]) -> dict[str, Any]:
    value = account.get("limit_status")
    return value if isinstance(value, dict) else {}


def _limit_status_short(account: dict[str, Any], language: str) -> str:
    status = _limit_status(account)
    if status.get("reached") is True:
        label = _pick(language, "limit reached", "лимит исчерпан")
        reset_at = status.get("reset_at")
        if isinstance(reset_at, (int, float)):
            label += f" · {_reset_short(reset_at, language)}"
        return label
    if status.get("unlimited") is True:
        return _pick(language, "unlimited", "без лимита")
    plan = account.get("plan")
    if isinstance(plan, str) and plan:
        return _pick(
            language,
            f"{plan} · allowance unavailable",
            f"{plan} · окно лимита недоступно",
        )
    return _pick(language, "allowance unavailable", "лимит недоступен")


def _account_block(
    account: dict[str, Any],
    *,
    active: bool,
    language: str,
    token_unit: tuple[int, str, int],
) -> str:
    marker = "●" if active else "○"
    if account.get("last_error") == "login required":
        marker = "⚠"
    plan = f" · {html.escape(str(account['plan']))}" if account.get("plan") else ""
    lines = [f"{marker} 👤 <b>{html.escape(account_name(account))}</b>{plan}"]
    target_count = account.get("target_count", 0)
    if target_count:
        label = _pick(language, "Hosts", "Хостов")
        lines.append(f"   🌐 {label}: <b>{target_count}</b>")
    email = account.get("email")
    if isinstance(email, str) and email:
        lines.append(f"   ✉ {html.escape(email)}")
    if account.get("last_error") == "login required":
        lines.append(
            _pick(
                language,
                "   🔑 Authentication required",
                "   🔑 Требуется повторный вход",
            )
        )
        lines.extend(_token_usage_block(account, language, token_unit))
        return "\n".join(lines)

    buckets = _limit_buckets(account)
    if not buckets:
        status = _limit_status(account)
        if status.get("reached") is True:
            lines.append(
                _pick(
                    language,
                    "   ⛔ Workspace limit reached",
                    "   ⛔ Лимит workspace исчерпан",
                )
            )
            reset_at = status.get("reset_at")
            if isinstance(reset_at, (int, float)):
                lines.append(
                    _pick(language, "      Resets ", "      Восстановится ")
                    + f"<b>{_reset_time(reset_at, language)}</b>"
                )
        elif status.get("unlimited") is True:
            lines.append(
                _pick(
                    language,
                    "   📊 Unlimited workspace allowance",
                    "   📊 Безлимитный workspace",
                )
            )
        else:
            lines.append(
                _pick(
                    language,
                    "   📊 Allowance window is unavailable",
                    "   📊 Окно лимита недоступно",
                )
            )
    for bucket in buckets:
        windows = [bucket.get("primary"), bucket.get("secondary")]
        for window in windows:
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if not isinstance(used, (int, float)):
                continue
            left = max(0, min(100, round(100 - used)))
            label = _duration_label(window, language)
            lines.append(
                f"   📊 {label}: <b>{left}%</b> · "
                f"{_reset_time(window.get('resetsAt'), language)}"
            )

    resets = _reset_count(account)
    lines.append(
        f"   ⚡ {_pick(language, 'Earned resets', 'Доступно reset')}: <b>{resets}</b>"
    )
    checked = parse_time(account.get("limits_checked_at"))
    if checked:
        age = max(0, round((dt.datetime.now(dt.UTC) - checked).total_seconds() / 60))
        if age > 1:
            lines.append(
                _pick(
                    language,
                    f"   🕒 <i>updated {age}m ago</i>",
                    f"   🕒 <i>данные {age} мин. назад</i>",
                )
            )
    if account.get("usage_error"):
        lines.append(
            _pick(
                language,
                "   🕒 <i>usage refresh delayed</i>",
                "   🕒 <i>обновление лимитов задерживается</i>",
            )
        )
    lines.extend(_token_usage_block(account, language, token_unit))
    return "\n".join(lines)


class TelegramBot:
    MAX_AUTH_FILE_BYTES = 1024 * 1024

    def __init__(self, openswap: OpenSwap, settings: TelegramSettings) -> None:
        self.openswap = openswap
        self.settings = settings
        self.base_url = f"https://api.telegram.org/bot{settings.token}/"
        self.menu_lock = threading.RLock()
        self.login_lock = threading.RLock()
        self.pending_resets: dict[int, PendingReset] = {}
        self.pending_deletes: dict[int, PendingDelete] = {}
        self.pending_logins: dict[int, PendingLogin] = {}
        self.login_choices: set[int] = set()
        self.import_prompts: set[int] = set()

    def run(self, stop: Any) -> None:
        self._install_commands()
        try:
            self.update_menus()
        except (TelegramError, OpenSwapError):
            pass
        offset = self.openswap.telegram_offset()
        try:
            while not stop.is_set():
                try:
                    updates = self._call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 30,
                            "allowed_updates": json.dumps(["message", "callback_query"]),
                        },
                        timeout=40,
                    )
                    if not isinstance(updates, list):
                        continue
                    for update in updates:
                        if not isinstance(update, dict):
                            continue
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            offset = max(offset, update_id + 1)
                        self._handle_update(update)
                    if updates:
                        self.openswap.set_telegram_offset(offset)
                except TelegramError as exc:
                    print(f"openswap: Telegram unavailable: {exc}", file=sys.stderr)
                    stop.wait(5)
                except (OpenSwapError, CodexError) as exc:
                    print(f"openswap: Telegram action failed: {exc}", file=sys.stderr)
                    stop.wait(2)
        finally:
            self._cancel_all_logins()

    def update_menus(self, notice: str | None = None) -> None:
        menus = self.openswap.telegram_menus()
        for raw_chat_id, menu in menus.items():
            try:
                chat_id = int(raw_chat_id)
                user_id = int(menu["user_id"])
                message_id = int(menu["message_id"])
                view_account = menu.get("view_account")
                if not isinstance(view_account, str):
                    view_account = None
            except (KeyError, TypeError, ValueError):
                continue
            if user_id not in self.settings.allowed_users:
                continue
            try:
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=notice,
                    allow_new=False,
                    view_account=view_account,
                )
            except TelegramError as exc:
                detail = str(exc).lower()
                if any(
                    marker in detail
                    for marker in (
                        "message to edit not found",
                        "message can't be edited",
                        "chat not found",
                        "bot was blocked",
                    )
                ):
                    self.openswap.clear_telegram_menu(chat_id)
                    continue
                print(
                    f"OpenSwap Telegram menu update failed: {exc}",
                    file=sys.stderr,
                )

    def create_menu(self, chat_id: int, user_id: int) -> None:
        self._cancel_login(chat_id)
        with self.menu_lock:
            self.pending_resets.pop(chat_id, None)
            self.pending_deletes.pop(chat_id, None)
            self.login_choices.discard(chat_id)
            self.import_prompts.discard(chat_id)
            stored = self.openswap.telegram_menus().get(str(chat_id))
            if isinstance(stored, dict) and isinstance(stored.get("message_id"), int):
                try:
                    self._call(
                        "deleteMessage",
                        {"chat_id": chat_id, "message_id": stored["message_id"]},
                    )
                except TelegramError:
                    pass
            self._show_menu(
                chat_id,
                user_id,
                allow_new=True,
                view_account=None,
                force_new=True,
            )

    def _handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("message"), dict):
            self._handle_message(update["message"])
        elif isinstance(update.get("callback_query"), dict):
            self._handle_callback(update["callback_query"])

    def _authorized(self, source: dict[str, Any], chat: dict[str, Any] | None) -> bool:
        user_id = source.get("id")
        return (
            isinstance(user_id, int)
            and user_id in self.settings.allowed_users
            and isinstance(chat, dict)
            and chat.get("type") == "private"
        )

    def _target_by_name(self, target_name: str) -> dict[str, Any]:
        targets = self.openswap.sync_status().get("targets", [])
        for target in targets:
            if target.get("name") == target_name:
                return target
        raise TelegramError("host is no longer configured")

    def _account_by_sequence(self, sequence: int) -> dict[str, Any]:
        accounts, _ = self.openswap.accounts()
        for account in accounts:
            if account.get("sequence") == sequence:
                return account
        raise TelegramError("session is no longer available")

    def _handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not self._authorized(sender, chat):
            return
        document = message.get("document")
        if isinstance(document, dict):
            self._handle_auth_document(message, sender, chat, document)
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/start":
            self.create_menu(int(chat["id"]), int(sender["id"]))
            return
        if command == "/language":
            message_id = message.get("message_id")
            if isinstance(message_id, int):
                try:
                    self._call(
                        "deleteMessage",
                        {"chat_id": int(chat["id"]), "message_id": message_id},
                    )
                except TelegramError:
                    pass
            self._show_menu(
                int(chat["id"]), int(sender["id"]), view_account="__language__"
            )
            return
        chat_id = int(chat["id"])
        pending = self.pending_logins.get(chat_id)
        language = self.openswap.telegram_language(int(sender["id"]))
        if (
            pending is not None
            and pending.mode == "browser"
            and text.strip().lower().startswith(
                ("http://localhost:", "http://127.0.0.1:")
            )
        ):
            message_id = message.get("message_id")
            if isinstance(message_id, int):
                try:
                    self._call(
                        "deleteMessage",
                        {"chat_id": chat_id, "message_id": message_id},
                    )
                except TelegramError:
                    pass
            try:
                pending.session.forward_callback(text.strip())
                self._show_menu(
                    chat_id,
                    int(sender["id"]),
                    notice=_pick(language, "Callback accepted; Codex is completing sign-in…", "Callback принят; Codex завершает вход…"),
                    view_account=None,
                )
            except CodexError as exc:
                self._show_menu(
                    chat_id,
                    int(sender["id"]),
                    notice=_pick(language, f"Callback error: {exc}", f"Ошибка callback: {exc}"),
                    view_account=None,
                )

    def _handle_auth_document(
        self,
        message: dict[str, Any],
        sender: dict[str, Any],
        chat: dict[str, Any],
        document: dict[str, Any],
    ) -> None:
        chat_id = int(chat["id"])
        user_id = int(sender["id"])
        language = self.openswap.telegram_language(user_id)
        message_id = message.get("message_id")
        file_id = document.get("file_id")
        file_name = document.get("file_name")
        file_size = document.get("file_size")
        if not isinstance(file_id, str):
            return
        try:
            if isinstance(file_name, str) and not file_name.lower().endswith(".json"):
                raise OpenSwapError(_pick(language, "the file must use the .json extension", "файл должен иметь расширение .json"))
            if isinstance(file_size, int) and file_size > self.MAX_AUTH_FILE_BYTES:
                raise OpenSwapError(_pick(language, "auth.json is too large", "auth.json слишком большой"))
            payload = self._download_file(file_id, language)
            try:
                auth_document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OpenSwapError(_pick(language, "the file is not valid UTF-8 JSON", "файл не является корректным UTF-8 JSON")) from exc
            result = self.openswap.import_auth_document(auth_document)
        except DeadSessionError:
            self.import_prompts.discard(chat_id)
            self._send_separate(
                chat_id,
                _pick(
                    language,
                    "⚠️ <b>Session not added</b>\n\nThe ChatGPT tokens are dead or the refresh token was revoked.",
                    "⚠️ <b>Сессия не добавлена</b>\n\nТокены этой ChatGPT-сессии мертвы или refresh token отозван.",
                ),
            )
            self._show_menu(
                chat_id,
                user_id,
                notice=_pick(language, "Dead session rejected", "Мёртвая сессия отклонена"),
                view_account=None,
            )
            return
        except (OpenSwapError, CodexError, TelegramError) as exc:
            self.import_prompts.discard(chat_id)
            self._send_separate(
                chat_id,
                _pick(language, "⚠️ <b>auth.json was not imported</b>", "⚠️ <b>auth.json не импортирован</b>")
                + f"\n\n{html.escape(str(exc))}",
            )
            self._show_menu(
                chat_id,
                user_id,
                notice=_pick(language, "auth.json rejected", "auth.json отклонён"),
                view_account=None,
            )
            return
        finally:
            if isinstance(message_id, int):
                try:
                    self._call(
                        "deleteMessage",
                        {"chat_id": chat_id, "message_id": message_id},
                    )
                except TelegramError:
                    pass
        notice = {
            "added": _pick(language, f"Added {account_name(result.account)}", f"Добавлена {account_name(result.account)}"),
            "replaced": _pick(language, f"{account_name(result.account)} updated with newer tokens", f"{account_name(result.account)} обновлена более новыми токенами"),
            "ignored": _pick(language, f"{account_name(result.account)} already has newer tokens", f"У {account_name(result.account)} уже более новые токены"),
        }[result.status]
        self.import_prompts.discard(chat_id)
        self._show_menu(
            chat_id,
            user_id,
            notice=notice,
            view_account=result.account["id"],
        )

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(sender, dict) or not self._authorized(sender, chat):
            return
        callback_id = callback.get("id")
        data = callback.get("data")
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if not isinstance(callback_id, str) or not isinstance(data, str) or not isinstance(message_id, int):
            return
        chat_id = int(chat["id"])
        user_id = int(sender["id"])
        language = self.openswap.telegram_language(user_id)
        self._answer(callback_id)

        try:
            if data.startswith("open:"):
                account_id = data.removeprefix("open:")
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=account_id,
                )
            elif data == "back":
                self.pending_resets.pop(chat_id, None)
                self.pending_deletes.pop(chat_id, None)
                self.login_choices.discard(chat_id)
                self.import_prompts.discard(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=None,
                )
            elif data.startswith("language:"):
                selected_language = data.removeprefix("language:")
                self.openswap.set_telegram_language(user_id, selected_language)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(selected_language, "Language changed", "Язык изменён"),
                    view_account=None,
                )
                self._install_chat_commands(chat_id, selected_language)
            elif data == "system":
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account="__system__",
                )
            elif data == "tokens":
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account="__tokens__",
                )
            elif data == "tokens-refresh":
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(
                        language,
                        "Refreshing token activity…",
                        "Обновляю активность токенов…",
                    ),
                    view_account="__tokens__",
                )
                self.openswap.refresh_all_token_usage(force=True)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(
                        language,
                        "Token activity refreshed",
                        "Активность токенов обновлена",
                    ),
                    view_account="__tokens__",
                )
            elif data == "retry-sync":
                self.openswap.request_sync()
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Sync scheduled", "Синхронизация запланирована"),
                    view_account="__system__",
                )
            elif data == "hosts":
                if self.openswap.sync_status().get("total", 0) <= 1:
                    self._show_menu(
                        chat_id,
                        user_id,
                        message_id=message_id,
                        view_account=None,
                    )
                    return
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account="__hosts__",
                )
            elif data.startswith("host:"):
                target_name = data.removeprefix("host:")
                self._target_by_name(target_name)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=f"__host__:{target_name}",
                )
            elif data.startswith("host-default:"):
                target_name = data.removeprefix("host-default:")
                target = self._target_by_name(target_name)
                assignment = self.openswap.unassign_target(target["name"])
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=f"{assignment['name']} → {_pick(language, 'default', 'по умолчанию')}",
                    view_account=f"__host__:{target_name}",
                )
            elif data.startswith("host-use:"):
                parts = data.split(":")
                if len(parts) != 3 or not parts[1] or not parts[2].isdigit():
                    raise TelegramError("invalid host assignment callback")
                target_name = parts[1]
                target = self._target_by_name(target_name)
                account = self._account_by_sequence(int(parts[2]))
                assignment = self.openswap.assign_target(
                    target["name"], account["id"]
                )
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=f"{assignment['name']} → {assignment['session']}",
                    view_account=f"__host__:{target_name}",
                )
            elif data == "all-default":
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account="__hosts_reset__",
                )
            elif data == "all-default-apply":
                removed = self.openswap.clear_target_overrides()
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(
                        language,
                        f"Overrides removed: {removed}",
                        f"Сброшено назначений: {removed}",
                    ),
                    view_account="__hosts__",
                )
            elif data == "add-session":
                self.login_choices.add(chat_id)
                self.import_prompts.discard(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=None,
                )
            elif data == "cancel-add":
                self.login_choices.discard(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=None,
                )
            elif data == "import-auth":
                self.login_choices.discard(chat_id)
                self.import_prompts.add(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=None,
                )
            elif data == "cancel-import":
                self.import_prompts.discard(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    view_account=None,
                )
            elif data == "login-device":
                self._start_login(chat_id, user_id, message_id, "device")
            elif data == "login-browser":
                self._start_login(chat_id, user_id, message_id, "browser")
            elif data.startswith("relogin-device:"):
                self._start_login(
                    chat_id,
                    user_id,
                    message_id,
                    "device",
                    replace_account=data.removeprefix("relogin-device:"),
                )
            elif data.startswith("relogin-browser:"):
                self._start_login(
                    chat_id,
                    user_id,
                    message_id,
                    "browser",
                    replace_account=data.removeprefix("relogin-browser:"),
                )
            elif data == "cancel-login":
                self._cancel_login(chat_id)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Sign-in cancelled", "Вход отменён"),
                    view_account=None,
                )
            elif data == "refresh-root":
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Refreshing all sessions…", "Обновляю все сессии…"),
                    view_account=None,
                )
                checked = self.openswap.refresh_all_usage()
                self.openswap.refresh_all_token_usage(force=True)
                notice = (
                    _pick(language, f"Updated: {len(checked)}", f"Обновлено: {len(checked)}")
                    if checked
                    else _pick(language, "No sessions to refresh", "Нет сессий для обновления")
                )
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=notice,
                    view_account=None,
                )
            elif data.startswith("use:"):
                account_id = data.removeprefix("use:")
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Switching…", "Переключаю…"),
                    view_account=account_id,
                )
                sync = self.openswap.sync_status()
                single_host = sync.get("enabled") and sync.get("total") == 1
                account = self.openswap.use(account_id, all_targets=bool(single_host))
                notice = _pick(
                    language,
                    f"Active session: {account_name(account)}" if single_host else f"Default: {account_name(account)}",
                    f"Активная сессия: {account_name(account)}" if single_host else f"По умолчанию: {account_name(account)}",
                )
                if sync["enabled"]:
                    notice += _pick(language, " · sync scheduled", " · синхронизация запланирована")
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=notice,
                    view_account=account_id,
                )
            elif data.startswith("refresh:"):
                account_id = data.removeprefix("refresh:")
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Refreshing…", "Обновляю…"),
                    view_account=account_id,
                )
                account = self.openswap.refresh(account_id)
                account = self.openswap.refresh_token_usage(
                    account_id, force=True
                )
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, f"Refreshed {account_name(account)}", f"Обновлён {account_name(account)}"),
                    view_account=account_id,
                )
            elif data.startswith("reset:"):
                account_id = data.removeprefix("reset:")
                self.pending_resets[chat_id] = PendingReset(
                    account_id=account_id,
                    idempotency_key=str(uuid.uuid4()),
                    expires_monotonic=time.monotonic() + 120,
                )
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "The nearest expiring reset will be used", "Будет использован reset с ближайшим сроком истечения"),
                    view_account=account_id,
                )
            elif data == "confirm-reset":
                pending = self.pending_resets.get(chat_id)
                if pending is None or pending.expires_monotonic < time.monotonic():
                    self.pending_resets.pop(chat_id, None)
                    self._show_menu(
                        chat_id,
                        user_id,
                        message_id=message_id,
                        notice=_pick(language, "Confirmation expired", "Подтверждение истекло"),
                        view_account=pending.account_id if pending else None,
                    )
                    return
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Applying reset…", "Применяю reset…"),
                    view_account=pending.account_id,
                )
                account, outcome = self.openswap.consume_reset(
                    pending.account_id, idempotency_key=pending.idempotency_key
                )
                self.pending_resets.pop(chat_id, None)
                outcome_text = {
                    "reset": _pick(language, f"Nearest reset applied to {account_name(account)}", f"Ближайший reset применён к {account_name(account)}"),
                    "alreadyRedeemed": _pick(language, "Reset was already applied; data refreshed", "Reset уже был применён; данные обновлены"),
                    "nothingToReset": _pick(language, "Nothing to reset now", "Сейчас нечего сбрасывать"),
                    "noCredit": _pick(language, "No earned reset credits", "Earned reset-кредитов нет"),
                }[outcome]
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=outcome_text,
                    view_account=pending.account_id,
                )
            elif data == "cancel-reset":
                pending = self.pending_resets.pop(chat_id, None)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Reset cancelled", "Reset отменён"),
                    view_account=pending.account_id if pending else None,
                )
            elif data.startswith("delete:"):
                account_id = data.removeprefix("delete:")
                self.pending_deletes[chat_id] = PendingDelete(
                    account_id=account_id,
                    expires_monotonic=time.monotonic() + 120,
                )
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Confirm permanent deletion", "Подтвердите необратимое удаление"),
                    view_account=account_id,
                )
            elif data == "confirm-delete":
                pending_delete = self.pending_deletes.get(chat_id)
                if (
                    pending_delete is None
                    or pending_delete.expires_monotonic < time.monotonic()
                ):
                    self.pending_deletes.pop(chat_id, None)
                    self._show_menu(
                        chat_id,
                        user_id,
                        message_id=message_id,
                        notice=_pick(language, "Deletion confirmation expired", "Подтверждение удаления истекло"),
                        view_account=(
                            pending_delete.account_id if pending_delete else None
                        ),
                    )
                    return
                account = self.openswap.remove_account(pending_delete.account_id)
                self.pending_deletes.pop(chat_id, None)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, f"{account_name(account)} deleted", f"{account_name(account)} удалена"),
                    view_account=None,
                )
            elif data == "cancel-delete":
                pending_delete = self.pending_deletes.pop(chat_id, None)
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Deletion cancelled", "Удаление отменено"),
                    view_account=(pending_delete.account_id if pending_delete else None),
                )
        except (OpenSwapError, CodexError, TelegramError) as exc:
            stored = self.openswap.telegram_menus().get(str(chat_id), {})
            stored_view = stored.get("view_account") if isinstance(stored, dict) else None
            if not isinstance(stored_view, str):
                stored_view = None
            self._show_menu(
                chat_id,
                user_id,
                message_id=message_id,
                notice=_pick(language, f"Error: {exc}", f"Ошибка: {exc}"),
                view_account=stored_view,
            )

    def _start_login(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        mode: str,
        *,
        replace_account: str | None = None,
    ) -> None:
        language = self.openswap.telegram_language(user_id)
        with self.login_lock:
            if self.pending_logins:
                self._show_menu(
                    chat_id,
                    user_id,
                    message_id=message_id,
                    notice=_pick(language, "Another sign-in is already running", "Другой вход уже выполняется"),
                    view_account=None,
                )
                return
            previous_auth: dict[str, Any] | None = None
            if replace_account is None:
                account_id, codex_home = self.openswap.begin_pending_account()
            else:
                account_id, codex_home, previous_auth = (
                    self.openswap.begin_account_login(replace_account)
                )
            try:
                session = self.openswap.codex.start_login(codex_home, mode)
            except BaseException:
                if previous_auth is None:
                    self.openswap.cancel_pending_account(account_id)
                else:
                    self.openswap.cancel_account_login(account_id, previous_auth)
                raise
            pending = PendingLogin(
                account_id=account_id,
                mode=mode,
                session=session,
                expires_monotonic=time.monotonic() + 15 * 60,
                previous_auth=previous_auth,
            )
            self.pending_logins[chat_id] = pending
            self.login_choices.discard(chat_id)
        self._show_menu(
            chat_id,
            user_id,
            message_id=message_id,
            view_account=None,
        )
        threading.Thread(
            target=self._wait_login,
            args=(chat_id, user_id, pending),
            daemon=True,
        ).start()

    def _wait_login(
        self, chat_id: int, user_id: int, pending: PendingLogin
    ) -> None:
        language = self.openswap.telegram_language(user_id)
        error: str | None = None
        try:
            pending.session.wait(timeout=15 * 60)
        except CodexError as exc:
            error = str(exc)
        finally:
            pending.session.close()
        with self.login_lock:
            if self.pending_logins.get(chat_id) is not pending:
                return
            self.pending_logins.pop(chat_id, None)
        if error is not None:
            self._rollback_login(pending)
            if pending.previous_auth is None:
                with self.menu_lock:
                    self.login_choices.add(chat_id)
            self._show_menu(
                chat_id,
                user_id,
                notice=_pick(
                    language,
                    f"Sign-in was not completed: {error}. Try another method or import auth.json.",
                    f"Вход не завершён: {error}. Попробуйте другой способ или импортируйте auth.json.",
                ),
                view_account=None,
            )
            return
        try:
            if pending.previous_auth is None:
                account = self.openswap.finalize_pending_account(pending.account_id)
            else:
                account = self.openswap.finalize_account_login(
                    pending.account_id, pending.previous_auth
                )
        except (OpenSwapError, CodexError) as exc:
            self._rollback_login(pending)
            if pending.previous_auth is None:
                with self.menu_lock:
                    self.login_choices.add(chat_id)
            action = _pick(
                language,
                "Session was not reauthorized" if pending.previous_auth is not None else "Session not added",
                "Сессия не переавторизована" if pending.previous_auth is not None else "Сессия не добавлена",
            )
            self._show_menu(
                chat_id,
                user_id,
                notice=f"{action}: {exc}",
                view_account=(pending.account_id if pending.previous_auth is not None else None),
            )
            return
        verb = _pick(
            language,
            "Reauthorized" if pending.previous_auth is not None else "Added",
            "Переавторизована" if pending.previous_auth is not None else "Добавлена",
        )
        self._show_menu(
            chat_id,
            user_id,
            notice=f"{verb} {account_name(account)}",
            view_account=account["id"],
        )

    def _cancel_login(self, chat_id: int) -> None:
        with self.login_lock:
            pending = self.pending_logins.pop(chat_id, None)
        if pending is None:
            return
        try:
            pending.session.cancel()
        except CodexError:
            pending.session.close()
        finally:
            self._rollback_login(pending)

    def _rollback_login(self, pending: PendingLogin) -> None:
        if pending.previous_auth is None:
            self.openswap.cancel_pending_account(pending.account_id)
        else:
            self.openswap.cancel_account_login(
                pending.account_id, pending.previous_auth
            )

    def _cancel_all_logins(self) -> None:
        with self.login_lock:
            chat_ids = list(self.pending_logins)
        for chat_id in chat_ids:
            self._cancel_login(chat_id)

    def _show_menu(
        self,
        chat_id: int,
        user_id: int,
        *,
        message_id: int | None = None,
        notice: str | None = None,
        allow_new: bool = True,
        view_account: str | None = None,
        force_new: bool = False,
    ) -> None:
        with self.menu_lock:
            stored = self.openswap.telegram_menus().get(str(chat_id))
            if force_new:
                message_id = None
            elif message_id is None and isinstance(stored, dict):
                candidate = stored.get("message_id")
                if isinstance(candidate, int):
                    message_id = candidate
            sync = self.openswap.sync_status()
            accounts, active = self.openswap.accounts()
            language = self.openswap.telegram_language(user_id)
            special_view = view_account in {
                "__hosts__",
                "__hosts_reset__",
                "__language__",
                "__system__",
                "__tokens__",
            }
            if isinstance(view_account, str) and view_account.startswith("__host__:"):
                target_name = view_account.removeprefix("__host__:")
                special_view = any(
                    target.get("name") == target_name for target in sync["targets"]
                )
                if not special_view:
                    view_account = "__hosts__"
                    special_view = True
            if view_account and not special_view and not any(
                account["id"] == view_account for account in accounts
            ):
                view_account = None
            menu_changed = not (
                isinstance(stored, dict)
                and stored.get("user_id") == user_id
                and stored.get("message_id") == message_id
                and stored.get("view_account") == view_account
            )
            pending = self.pending_resets.get(chat_id)
            if pending and pending.expires_monotonic < time.monotonic():
                self.pending_resets.pop(chat_id, None)
                pending = None
            pending_delete = self.pending_deletes.get(chat_id)
            if (
                pending_delete
                and pending_delete.expires_monotonic < time.monotonic()
            ):
                self.pending_deletes.pop(chat_id, None)
                pending_delete = None
            with self.login_lock:
                pending_login = self.pending_logins.get(chat_id)
            login_choice = chat_id in self.login_choices
            import_prompt = chat_id in self.import_prompts
            system_status = (
                self.openswap.system_status() if view_account == "__system__" else None
            )
            text = self._menu_text(
                accounts,
                active,
                notice,
                view_account,
                pending_login,
                login_choice,
                import_prompt,
                sync,
                language,
                system_status,
            )
            keyboard = self._menu_keyboard(
                accounts,
                active,
                pending,
                pending_delete,
                pending_login,
                login_choice,
                import_prompt,
                view_account,
                sync,
                language,
            )
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": json.dumps({"inline_keyboard": keyboard}),
            }
            if message_id is not None:
                payload["message_id"] = message_id
                try:
                    self._call("editMessageText", payload)
                    if menu_changed:
                        self.openswap.set_telegram_menu(
                            chat_id, user_id, message_id, view_account
                        )
                    return
                except TelegramError as exc:
                    detail = str(exc).lower()
                    if "message is not modified" in detail:
                        return
                    if not allow_new or not any(
                        marker in detail
                        for marker in ("message to edit not found", "message can't be edited")
                    ):
                        raise
            result = self._call("sendMessage", payload)
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                raise TelegramError("sendMessage returned no message ID")
            self.openswap.set_telegram_menu(
                chat_id, user_id, result["message_id"], view_account
            )

    @staticmethod
    def _menu_text(
        accounts: list[dict[str, Any]],
        active: str | None,
        notice: str | None,
        view_account: str | None,
        pending_login: PendingLogin | None,
        login_choice: bool,
        import_prompt: bool,
        sync: dict[str, Any],
        language: str,
        system_status: dict[str, Any] | None,
    ) -> str:
        effective_active = active
        if sync.get("enabled") and sync.get("total") == 1 and sync.get("targets"):
            effective_active = sync["targets"][0].get("account_id")
        selected = next(
            (account for account in accounts if account["id"] == view_account), None
        )
        token_overview = token_usage_overview(accounts)
        token_unit = _token_unit(token_overview)
        host_target = None
        if isinstance(view_account, str) and view_account.startswith("__host__:"):
            host_name = view_account.removeprefix("__host__:")
            host_target = next(
                (target for target in sync["targets"] if target["name"] == host_name),
                None,
            )
        if view_account == "__language__":
            lines = [f"🗣 <b>OpenSwap</b> · {_pick(language, 'language', 'язык')}"]
        elif view_account == "__system__":
            lines = [f"⚙ <b>OpenSwap</b> · {_pick(language, 'system', 'система')}"]
        elif view_account == "__tokens__":
            lines = [
                "📊 <b>OpenSwap</b> · "
                + _pick(language, "token activity", "активность токенов")
            ]
        elif view_account in {"__hosts__", "__hosts_reset__"}:
            lines = [f"🌐 <b>OpenSwap</b> · {_pick(language, 'hosts', 'хосты')}"]
        elif host_target is not None:
            lines = [f"🖥 <b>OpenSwap</b> · {_pick(language, 'assignment', 'назначение')}"]
        elif pending_login or login_choice or import_prompt:
            lines = [f"➕ <b>OpenSwap</b> · {_pick(language, 'add session', 'добавить сессию')}"]
        elif selected:
            lines = [f"👤 <b>OpenSwap</b> · {_pick(language, 'session', 'сессия')}"]
        else:
            lines = [
                "🔐 <b>OpenSwap</b> · "
                + _pick(language, f"sessions: {len(accounts)}", f"сессий: {len(accounts)}")
            ]
        if notice:
            lines.extend([f"<i>{html.escape(notice)}</i>", ""])
        else:
            lines.append("")
        if view_account == "__language__":
            lines.extend(
                [
                    _pick(language, "Choose the interface language.", "Выберите язык интерфейса."),
                    _pick(
                        language,
                        "This setting is saved for your Telegram account.",
                        "Настройка сохраняется для вашего Telegram-аккаунта.",
                    ),
                    "",
                ]
            )
        elif view_account == "__system__" and system_status is not None:
            codex = system_status["codex"]
            opencode = system_status["opencode"]
            sessions = system_status["sessions"]
            system_sync = system_status["sync"]
            opencode_ready = opencode["status"] == "ready"
            codex_label = codex.get("version") or _pick(
                language, "unavailable", "недоступен"
            )
            lines.extend(
                [
                    f"<b>OpenSwap {html.escape(system_status['version'])}</b>",
                    "",
                    f"{_status_icon(opencode_ready)} 🔐 OpenCode · "
                    + _pick(
                        language,
                        opencode["status"].replace("_", " "),
                        {
                            "ready": "готов",
                            "missing": "файл не найден",
                            "invalid": "ошибка файла",
                            "unsafe_permissions": "небезопасные права",
                        }.get(opencode["status"], opencode["status"]),
                    ),
                    f"{_status_icon(codex['ok'])} 🤖 Codex · {html.escape(codex_label)}",
                    f"{_status_icon(system_status['storage']['ok'])} "
                    + _pick(language, "💾 Storage", "💾 Хранилище"),
                    f"{_status_icon(sessions['healthy'] == sessions['total'])} "
                    + _pick(language, "👤 Sessions", "👤 Сессии")
                    + f" · {sessions['healthy']}/{sessions['total']}",
                    f"{_status_icon(sessions['usage_fresh'] == sessions['healthy'])} "
                    + _pick(language, "📊 Usage freshness", "📊 Свежесть лимитов")
                    + f" · {sessions['usage_fresh']}/{sessions['healthy']}",
                    f"{_status_icon(sessions['token_usage_fresh'] == sessions['healthy'])} "
                    + _pick(
                        language,
                        "🧮 Token activity",
                        "🧮 Активность токенов",
                    )
                    + f" · {sessions['token_usage_fresh']}/{sessions['healthy']}",
                ]
            )
            if system_sync["enabled"]:
                lines.append(
                    f"{_status_icon(system_sync['synced'] == system_sync['total'])} "
                    + _pick(language, "🌐 Hosts", "🌐 Хосты")
                    + f" · {system_sync['synced']}/{system_sync['total']}"
                )
            else:
                lines.append(
                    "✓ " + _pick(language, "🏠 Single-host mode", "🏠 Режим одного хоста")
                )
            if system_status["issues"]:
                lines.extend(["", f"<b>⚠ {_pick(language, 'Attention', 'Требует внимания')}</b>"])
                lines.extend(
                    f"• {html.escape(_system_issue_label(language, issue))}"
                    for issue in system_status["issues"]
                )
            else:
                lines.extend(
                    [
                        "",
                        _pick(
                            language,
                            "Everything is operating normally.",
                            "Всё работает штатно.",
                        ),
                    ]
                )
            lines.append("")
        elif view_account == "__tokens__":
            overview = token_overview
            unit_label = _token_unit_description(token_unit, language)
            lines.extend(
                [
                    f"<b>{_pick(language, 'Total · available accounts', 'Итого · доступные аккаунты')}</b>"
                    + f": {overview['available']}/{overview['total']}",
                    _pick(language, "Last 7 days", "Последние 7 дней")
                    + f": ≈ <b>{_format_token_total(overview, 'seven_days', token_unit, language)}</b>",
                    _pick(language, "Last 30 days", "Последние 30 дней")
                    + f": ≈ <b>{_format_token_total(overview, 'thirty_days', token_unit, language)}</b>",
                    _pick(language, "All time", "За всё время")
                    + f": ≈ <b>{_format_token_total(overview, 'lifetime', token_unit, language)}</b>",
                    "",
                    f"<b>{_pick(language, 'By session · 7d / 30d / all time', 'По сессиям · 7 д. / 30 д. / всё время')}</b>",
                    _pick(language, "Unit", "Единица")
                    + f": {unit_label} · "
                    + _pick(language, "rounded", "округлено"),
                ]
            )
            for row in overview["rows"]:
                account = row["account"]
                stats = row["stats"]
                label = html.escape(account_name(account))
                if not stats["available"]:
                    reason = (
                        _pick(language, " · login required", " · нужен вход")
                        if account.get("last_error") == "login required"
                        else ""
                    )
                    lines.append(
                        f"• {label} · <i>"
                        + _pick(language, "no data", "нет данных")
                        + reason
                        + "</i>"
                    )
                    continue
                stale = " ⚠" if stats["stale"] else ""
                lines.append(
                    f"• {label} · "
                    f"{_format_token_value(stats['seven_days'], token_unit, language, suffix=False)} / "
                    f"{_format_token_value(stats['thirty_days'], token_unit, language, suffix=False)} / "
                    f"{_format_token_value(stats['lifetime'], token_unit, language, suffix=False)}{stale}"
                )
            lines.extend(
                [
                    "",
                    (
                        f"<b>Σ {_pick(language, 'Total', 'Итого')} ≈</b> · "
                        f"{_format_token_total(overview, 'seven_days', token_unit, language, suffix=False)} / "
                        f"{_format_token_total(overview, 'thirty_days', token_unit, language, suffix=False)} / "
                        f"{_format_token_total(overview, 'lifetime', token_unit, language, suffix=False)}"
                    ),
                    _pick(
                        language,
                        "<i>Σ adds the rounded rows shown above.</i>",
                        "<i>Σ складывает показанные выше округлённые строки.</i>",
                    ),
                ]
            )
            if overview["oldest_checked_at"] is not None:
                lines.append(
                    _pick(language, "Oldest data", "Самые старые данные")
                    + f": <i>{_token_age(overview['oldest_checked_at'], language)}</i>"
                )
            lines.extend(
                [
                    "",
                    _pick(
                        language,
                        "Per ChatGPT account across all Codex apps/devices, including configured hosts. Host assignments are not multiplied. This is not billing or remaining allowance.",
                        "По каждому ChatGPT-аккаунту во всех приложениях/устройствах Codex, включая настроенные хосты. Назначения хостов не умножают значения. Это не биллинг и не остаток лимита.",
                    ),
                    "",
                ]
            )
        elif view_account == "__hosts__":
            if sync.get("default_session"):
                lines.append(
                    _pick(language, "🎯 Default: ", "🎯 По умолчанию: ")
                    + f"<b>{html.escape(sync['default_session'])}</b>"
                )
            lines.append(
                _pick(language, "🔀 Overrides", "🔀 Индивидуально")
                + f": <b>{sync.get('override_count', 0)}</b>"
            )
            lines.append("")
            for target in sync["targets"]:
                status = target.get("status")
                icon = {
                    "synced": "✓",
                    "offline": "○",
                    "error": "!",
                    "empty": "·",
                    "pending": "↻",
                }.get(status, "·")
                label = target.get("session") or _pick(language, "unassigned", "не назначена")
                if target.get("override"):
                    label += _pick(language, " · override", " · отдельно")
                if status != "synced":
                    label += " · " + {
                        "pending": _pending_label(language, target.get("error")),
                        "offline": _pick(language, "offline", "недоступен"),
                        "error": _pick(language, "error", "ошибка"),
                        "empty": _pick(language, "no session", "нет сессии"),
                    }.get(status, _pick(language, "unknown", "неизвестно"))
                lines.append(
                    f"{icon} <b>{html.escape(target['name'])}</b> · "
                    f"{html.escape(str(label))}"
                )
            lines.append("")
        elif view_account == "__hosts_reset__":
            lines.extend(
                [
                    _pick(language, "Apply the default session to every host?", "Вернуть все хосты к сессии по умолчанию?"),
                    _pick(language, "All overrides will be removed.", "Индивидуальные назначения будут удалены."),
                    _pick(language, "Credentials and sessions are not deleted.", "Credentials и сами сессии не удаляются."),
                    "",
                ]
            )
        elif host_target is not None:
            target = host_target
            status = target.get("status")
            status_label = {
                "synced": _pick(language, "synced", "синхронизирован"),
                "pending": _pending_label(language, target.get("error")),
                "offline": _pick(language, "offline", "недоступен"),
                "error": _pick(language, "error", "ошибка"),
                "empty": _pick(language, "no session", "нет сессии"),
            }.get(status, _pick(language, "unknown", "неизвестно"))
            mode = _pick(language, "override", "индивидуально") if target.get("override") else _pick(language, "default", "по умолчанию")
            lines.extend(
                [
                    f"🖥 <b>{html.escape(target['name'])}</b>",
                    f"👤 {_pick(language, 'Session', 'Сессия')}: <b>{html.escape(target.get('session') or '—')}</b>",
                    f"🔀 {_pick(language, 'Mode', 'Режим')}: {mode}",
                    f"🔄 {_pick(language, 'Status', 'Состояние')}: {status_label}",
                    "",
                    _pick(language, "Choose the session for this host.", "Выберите рабочую сессию для этого хоста."),
                ]
            )
            unavailable = [
                account_name(account)
                for account in accounts
                if account.get("last_error") == "login required"
            ]
            if unavailable:
                lines.append(
                    _pick(language, "Login required: ", "Требуют входа: ")
                    + ", ".join(html.escape(name) for name in unavailable)
                )
            lines.append("")
        elif pending_login:
            remaining = max(
                0, round((pending_login.expires_monotonic - time.monotonic()) / 60)
            )
            if pending_login.mode == "device":
                lines.extend(
                    [
                        f"{_pick(language, 'Method', 'Способ')}: <b>Device Code</b>",
                        f"{_pick(language, 'Code', 'Код')}: <code>{html.escape(pending_login.session.prompt.user_code or '')}</code>",
                        _pick(language, "Open the page below and enter the code.", "Откройте страницу кнопкой ниже и введите код."),
                    ]
                )
            else:
                lines.extend(
                    [
                        f"{_pick(language, 'Method', 'Способ')}: <b>Browser OAuth</b>",
                        _pick(language, "Open the link below and complete sign-in.", "Откройте ссылку кнопкой ниже и завершите вход."),
                        _pick(language, "After the localhost redirect, send the full URL here.", "После перехода на localhost отправьте полный URL сюда."),
                        _pick(language, "The callback message will be deleted immediately.", "Сообщение с callback будет сразу удалено."),
                    ]
                )
            lines.extend([_pick(language, f"Expires in about {remaining}m.", f"Истекает примерно через {remaining} мин."), ""])
        elif login_choice:
            lines.extend(
                [
                    _pick(language, "Choose an official Codex sign-in method.", "Выберите официальный способ входа Codex."),
                    _pick(
                        language,
                        "Device Code requires workspace permission; Browser supports SSO; Codex CLI auth.json is the reliable fallback.",
                        "Device Code требует разрешения workspace; Browser поддерживает SSO; auth.json из Codex CLI — надёжный запасной способ.",
                    ),
                    "",
                ]
            )
        elif import_prompt:
            lines.extend(
                [
                    _pick(language, "Send <code>auth.json</code> as a document.", "Отправьте <code>auth.json</code> как документ."),
                    _pick(
                        language,
                        "Codex CLI, OpenCode, and OpenCodez auth.json files are supported.",
                        "Поддерживаются auth.json из Codex CLI, OpenCode и OpenCodez.",
                    ),
                    _pick(language, "The credential message will be deleted after download.", "Credential-сообщение будет удалено после скачивания."),
                    "",
                ]
            )
        elif selected:
            lines.append(
                _account_block(
                    selected,
                    active=selected["id"] == effective_active,
                    language=language,
                    token_unit=token_unit,
                )
            )
            lines.append("")
        elif accounts:
            active_account = next(
                (account for account in accounts if account["id"] == effective_active),
                None,
            )
            if active_account:
                if len(accounts) == 1:
                    lines.append(
                        _pick(language, "👤 Session: ", "👤 Сессия: ")
                        + f"<b>{html.escape(account_name(active_account))}</b>"
                    )
                else:
                    lines.append(
                        _pick(
                            language,
                            "🎯 Active: " if sync.get("total") == 1 else "🎯 Default: ",
                            "🎯 Активная: " if sync.get("total") == 1 else "🎯 По умолчанию: ",
                        )
                        + f"<b>{html.escape(account_name(active_account))}</b>"
                    )
            if sync["enabled"]:
                if sync["total"] == 1:
                    target = sync["targets"][0]
                    status_label = {
                        "synced": "✓",
                        "pending": _pending_label(
                            language, target.get("error"), icon=True
                        ),
                        "offline": _pick(language, "⚠ offline", "⚠ недоступен"),
                        "error": _pick(language, "⚠ error", "⚠ ошибка"),
                        "empty": _pick(language, "⚠ no session", "⚠ нет сессии"),
                    }.get(
                        target.get("status"),
                        _pick(language, "⚠ unknown", "⚠ неизвестно"),
                    )
                    lines.append(
                        f"🔄 {_pick(language, 'Sync', 'Синхронизация')}: "
                        f"<b>{html.escape(target['name'])}</b> {status_label}"
                    )
                else:
                    lines.append(
                        f"🌐 {_pick(language, 'Hosts', 'Хосты')}: <b>{sync['synced']}/{sync['total']}</b>"
                        f" · 🔀 {_pick(language, 'overrides', 'индивидуально')}: <b>{sync.get('override_count', 0)}</b>"
                    )
                    if len(accounts) > 1:
                        assignments = [
                            account
                            for account in accounts
                            if int(account.get("target_count") or 0) > 0
                        ]
                        if assignments:
                            lines.extend(
                                [
                                    "",
                                    f"<b>🖥 {_pick(language, 'Host assignments', 'Назначения хостов')}</b>",
                                ]
                            )
                            lines.extend(
                                f"• 👤 {html.escape(account_name(account))} · "
                                f"{_host_count_label(int(account.get('target_count') or 0), language)}"
                                for account in assignments
                            )
            overview = token_overview
            if overview["available"]:
                lines.extend(
                    [
                        "",
                        "📊 "
                        + _pick(language, "Token activity", "Активность токенов")
                        + f" · {overview['available']}/{overview['total']} "
                        + _pick(language, "accounts", "аккаунтов"),
                    _pick(language, "Last 7 days", "Последние 7 дней")
                        + f" ≈ <b>{_format_token_total(overview, 'seven_days', token_unit, language)}</b>"
                        + _pick(language, " · last 30 days ", " · последние 30 дней ")
                        + f"≈ <b>{_format_token_total(overview, 'thirty_days', token_unit, language)}</b>",
                    ]
                )
            else:
                lines.extend(
                    [
                        "",
                        "📊 "
                        + _pick(
                            language,
                            "Token activity · collecting data",
                            "Активность токенов · данные собираются",
                        ),
                    ]
                )
            lines.append("")
            lines.extend([_pick(language, "Choose a session for details and actions.", "Выберите сессию для подробностей и действий."), ""])
        else:
            lines.extend([_pick(language, "No sessions yet", "Аккаунтов пока нет"), ""])
        lines.append(
            f"🕒 {_pick(language, 'Updated', 'Обновлено')} "
            f"{dt.datetime.now().astimezone():%H:%M}"
        )
        return "\n".join(lines)

    @staticmethod
    def _menu_keyboard(
        accounts: list[dict[str, Any]],
        active: str | None,
        pending: PendingReset | None,
        pending_delete: PendingDelete | None,
        pending_login: PendingLogin | None,
        login_choice: bool,
        import_prompt: bool,
        view_account: str | None,
        sync: dict[str, Any],
        language: str,
    ) -> list[list[dict[str, str]]]:
        effective_active = active
        if sync.get("enabled") and sync.get("total") == 1 and sync.get("targets"):
            effective_active = sync["targets"][0].get("account_id")
        if view_account == "__language__":
            return [
                [{"text": ("✓ " if language == "en" else "") + "English", "callback_data": "language:en"}],
                [{"text": ("✓ " if language == "ru" else "") + "Русский", "callback_data": "language:ru"}],
                [{"text": _pick(language, "← Back", "← Назад"), "callback_data": "back"}],
            ]
        if view_account == "__system__":
            actions = [
                {
                    "text": _pick(language, "↻ Refresh", "↻ Обновить"),
                    "callback_data": "system",
                }
            ]
            if sync["enabled"]:
                actions.insert(
                    0,
                    {
                        "text": _pick(language, "↻ Retry sync", "↻ Повторить sync"),
                        "callback_data": "retry-sync",
                    },
                )
            return [
                actions,
                [
                    {
                        "text": _pick(language, "← All sessions", "← Все сессии"),
                        "callback_data": "back",
                    }
                ],
            ]
        if view_account == "__tokens__":
            return [
                [
                    {
                        "text": _pick(language, "↻ Refresh", "↻ Обновить"),
                        "callback_data": "tokens-refresh",
                    }
                ],
                [
                    {
                        "text": _pick(language, "← All sessions", "← Все сессии"),
                        "callback_data": "back",
                    }
                ],
            ]
        if view_account == "__hosts__":
            keyboard: list[list[dict[str, str]]] = []
            for target in sync["targets"]:
                icon = {
                    "synced": "✓",
                    "pending": "↻",
                    "offline": "○",
                    "error": "!",
                }.get(target.get("status"), "·")
                suffix = _pick(language, " · override", " · отдельно") if target.get("override") else ""
                keyboard.append(
                    [
                        {
                            "text": (
                                f"{icon} {target['name']} · "
                                f"{target.get('session') or '—'}{suffix}"
                            ),
                            "callback_data": f"host:{target['name']}",
                        }
                    ]
                )
            if sync.get("override_count"):
                keyboard.append(
                    [
                        {
                            "text": _pick(language, "Default → all", "По умолчанию → всем"),
                            "callback_data": "all-default",
                        }
                    ]
                )
            keyboard.append([{"text": _pick(language, "← All sessions", "← Все сессии"), "callback_data": "back"}])
            return keyboard
        if view_account == "__hosts_reset__":
            return [
                [
                    {
                        "text": _pick(language, "Apply to all", "Применить ко всем"),
                        "callback_data": "all-default-apply",
                    },
                    {"text": _pick(language, "Cancel", "Отмена"), "callback_data": "hosts"},
                ]
            ]
        if isinstance(view_account, str) and view_account.startswith("__host__:"):
            target_name = view_account.removeprefix("__host__:")
            target = next(
                target for target in sync["targets"] if target["name"] == target_name
            )
            keyboard = []
            for account in accounts:
                if account.get("last_error") == "login required":
                    continue
                is_selected = account["id"] == target.get("account_id")
                is_default = account["id"] == sync.get("default_account")
                marker = "✓ " if is_selected else ""
                suffix = _pick(language, " · default", " · по умолчанию") if is_default else ""
                callback = (
                    f"host-default:{target_name}"
                    if is_default
                    else f"host-use:{target_name}:{account['sequence']}"
                )
                keyboard.append(
                    [
                        {
                            "text": f"{marker}{account_name(account)}{suffix}",
                            "callback_data": callback,
                        }
                    ]
                )
            keyboard.append([{"text": _pick(language, "← Hosts", "← Хосты"), "callback_data": "hosts"}])
            return keyboard
        if pending_login:
            prompt = pending_login.session.prompt
            login_url = (
                prompt.verification_url
                if pending_login.mode == "device"
                else prompt.auth_url
            )
            keyboard: list[list[dict[str, str]]] = []
            if login_url:
                keyboard.append(
                    [{"text": _pick(language, "Open sign-in page", "Открыть страницу входа"), "url": login_url}]
                )
            keyboard.append(
                [{"text": _pick(language, "Cancel sign-in", "Отменить вход"), "callback_data": "cancel-login"}]
            )
            return keyboard
        if login_choice:
            return [
                [
                    {
                        "text": _pick(language, "Device Code · recommended", "Device Code · рекомендуется"),
                        "callback_data": "login-device",
                    }
                ],
                [{"text": "Browser OAuth", "callback_data": "login-browser"}],
                [{"text": _pick(language, "Import auth.json", "Импорт auth.json"), "callback_data": "import-auth"}],
                [{"text": _pick(language, "← Back", "← Назад"), "callback_data": "cancel-add"}],
            ]
        if import_prompt:
            return [[{"text": _pick(language, "← Back", "← Назад"), "callback_data": "cancel-import"}]]
        if pending_delete:
            return [
                [
                    {
                        "text": _pick(language, "🗑 Confirm deletion", "🗑 Подтвердить удаление"),
                        "callback_data": "confirm-delete",
                    },
                    {"text": _pick(language, "Cancel", "Отмена"), "callback_data": "cancel-delete"},
                ]
            ]
        if pending:
            return [
                [
                    {"text": _pick(language, "⚡ Use nearest", "⚡ Использовать ближайший"), "callback_data": "confirm-reset"},
                    {"text": _pick(language, "Cancel", "Отмена"), "callback_data": "cancel-reset"},
                ]
            ]
        selected = next(
            (account for account in accounts if account["id"] == view_account), None
        )
        if selected:
            keyboard: list[list[dict[str, str]]] = []
            if selected.get("last_error") == "login required":
                keyboard.append(
                    [
                        {
                            "text": "Device Code",
                            "callback_data": f"relogin-device:{selected['id']}",
                        },
                        {
                            "text": "Browser OAuth",
                            "callback_data": f"relogin-browser:{selected['id']}",
                        },
                    ]
                )
            elif not selected.get("last_error"):
                if selected["id"] != effective_active:
                    keyboard.append(
                        [
                            {
                                "text": _pick(
                                    language,
                                    "Use on this host" if sync.get("total") == 1 else "Make default",
                                    "Использовать на этом хосте" if sync.get("total") == 1 else "Сделать по умолчанию",
                                ),
                                "callback_data": f"use:{selected['id']}",
                            }
                        ]
                    )
                keyboard.append(
                    [
                        {
                            "text": _pick(language, "↻ Refresh", "↻ Обновить"),
                            "callback_data": f"refresh:{selected['id']}",
                        }
                    ]
                )
                resets = _reset_count(selected)
                if resets:
                    keyboard.append(
                        [
                            {
                                "text": f"⚡ Reset · {resets}",
                                "callback_data": f"reset:{selected['id']}",
                            }
                        ]
                    )
            if selected["id"] != active and not selected.get("target_count"):
                keyboard.append(
                    [
                        {
                            "text": _pick(language, "🗑 Delete session", "🗑 Удалить сессию"),
                            "callback_data": f"delete:{selected['id']}",
                        }
                    ]
                )
            keyboard.append([{"text": _pick(language, "← All sessions", "← Все сессии"), "callback_data": "back"}])
            return keyboard
        keyboard = [
            [
                {
                    "text": _root_label(
                        account,
                        active=account["id"] == effective_active,
                        language=language,
                    ),
                    "callback_data": f"open:{account['id']}",
                }
            ]
            for account in accounts
        ]
        keyboard.append(
            [
                {"text": _pick(language, "＋ Add", "＋ Добавить"), "callback_data": "add-session"},
                {"text": _pick(language, "↻ Refresh", "↻ Обновить"), "callback_data": "refresh-root"},
            ]
        )
        if accounts:
            keyboard.append(
                [
                    {
                        "text": _pick(
                            language,
                            "📊 Token activity",
                            "📊 Активность токенов",
                        ),
                        "callback_data": "tokens",
                    }
                ]
            )
        system_button = {
            "text": _pick(language, "⚙ System", "⚙ Система"),
            "callback_data": "system",
        }
        if sync["enabled"] and sync["total"] > 1:
            keyboard.append(
                [
                    system_button,
                    {
                        "text": f"🌐 {_pick(language, 'Hosts', 'Хосты')} · {sync['synced']}/{sync['total']}",
                        "callback_data": "hosts",
                    },
                ]
            )
        else:
            keyboard.append([system_button])
        return keyboard

    def _answer(self, callback_id: str) -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        except TelegramError:
            pass

    def _install_chat_commands(self, chat_id: int, language: str) -> None:
        descriptions = (
            ("Открыть OpenSwap", "Язык")
            if language == "ru"
            else ("Open OpenSwap", "Language")
        )
        try:
            self._call(
                "setMyCommands",
                {
                    "commands": json.dumps(
                        [
                            {"command": "start", "description": descriptions[0]},
                            {"command": "language", "description": descriptions[1]},
                        ]
                    ),
                    "scope": json.dumps({"type": "chat", "chat_id": chat_id}),
                },
            )
        except TelegramError:
            pass

    def _install_commands(self) -> None:
        try:
            self._call(
                "setMyCommands",
                {
                    "commands": json.dumps(
                        [
                            {"command": "start", "description": "Open OpenSwap"},
                            {"command": "language", "description": "Language"},
                        ]
                    )
                },
            )
            self._call(
                "setMyCommands",
                {
                    "commands": json.dumps(
                        [
                            {"command": "start", "description": "Открыть OpenSwap"},
                            {"command": "language", "description": "Язык"},
                        ]
                    ),
                    "language_code": "ru",
                },
            )
        except TelegramError:
            pass

    def _send_separate(self, chat_id: int, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def _download_file(self, file_id: str, language: str) -> bytes:
        result = self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(
            result.get("file_path"), str
        ):
            raise TelegramError("Telegram returned no downloadable file path")
        file_path = result["file_path"].lstrip("/")
        parts = file_path.split("/")
        if not file_path or ".." in parts or "\\" in file_path:
            raise TelegramError("Telegram returned an invalid file path")
        safe_path = urllib.parse.quote(file_path, safe="/")
        request = urllib.request.Request(
            f"https://api.telegram.org/file/bot{self.settings.token}/{safe_path}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.MAX_AUTH_FILE_BYTES:
                    raise OpenSwapError(_pick(language, "auth.json is too large", "auth.json слишком большой"))
                payload = response.read(self.MAX_AUTH_FILE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"file download failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TelegramError("file download failed") from None
        if len(payload) > self.MAX_AUTH_FILE_BYTES:
            raise OpenSwapError(_pick(language, "auth.json is too large", "auth.json слишком большой"))
        return payload

    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 15,
    ) -> Any:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + method, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("description", f"HTTP {exc.code}")
            except (ValueError, json.JSONDecodeError):
                detail = f"HTTP {exc.code}"
            raise TelegramError(str(detail)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramError("network error") from None
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("description", "API error") if isinstance(result, dict) else "API error"
            raise TelegramError(str(detail))
        return result.get("result")
