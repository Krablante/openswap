from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Any

from .codex import CodexError
from .config import ConfigError, load_config
from .core import OpenSwap, OpenSwapError
from .telegram import TelegramBot, TelegramError


def main() -> int:
    if len(sys.argv) > 1:
        print(
            "openswap: commands are not supported; edit config.toml and use Telegram",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_config()
        openswap = OpenSwap(config.core)
        openswap.initialize()
    except (ConfigError, OpenSwapError) as exc:
        print(f"openswap: {exc}", file=sys.stderr)
        return 1

    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        openswap.wake()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    bot = TelegramBot(openswap, config.telegram)
    telegram_thread = threading.Thread(target=bot.run, args=(stop,), daemon=True)
    telegram_thread.start()
    print(f"openswap: started with {config.path}", flush=True)

    next_refresh = time.monotonic()
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_refresh:
            try:
                result = openswap.refresh_scheduler_data(
                    max_age_seconds=max(
                        1, config.scheduler_interval_seconds - 5
                    )
                )
                if result["refreshed"]:
                    print(
                        f"openswap: refreshed {', '.join(result['refreshed'])}",
                        flush=True,
                    )
                for error in result["errors"]:
                    print(
                        f"openswap: usage warning: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
            except (OpenSwapError, CodexError) as exc:
                print(
                    f"openswap: refresh warning: {exc}", file=sys.stderr, flush=True
                )
            try:
                openswap.refresh_all_token_usage(
                    max_age_seconds=(
                        openswap.settings.token_usage_refresh_seconds
                    )
                )
            except (OpenSwapError, CodexError) as exc:
                print(
                    f"openswap: token usage warning: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            next_refresh += config.scheduler_interval_seconds
            if next_refresh <= time.monotonic():
                next_refresh = time.monotonic() + config.scheduler_interval_seconds

        try:
            openswap.sync_targets(force=False)
        except (OpenSwapError, CodexError) as exc:
            print(f"openswap: sync warning: {exc}", file=sys.stderr, flush=True)

        try:
            bot.update_menus()
        except (OpenSwapError, TelegramError) as exc:
            print(f"openswap: Telegram warning: {exc}", file=sys.stderr, flush=True)

        openswap.wait_for_work(max(0.0, next_refresh - time.monotonic()))

    telegram_thread.join(timeout=2)
    print("openswap: stopped", flush=True)
    return 0
