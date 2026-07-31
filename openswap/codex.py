"""Small JSONL client for the official Codex App Server."""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__


class CodexError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexSnapshot:
    account: dict[str, Any] | None
    rate_limits: dict[str, Any] | None
    rate_limits_by_id: dict[str, Any] | None = None
    reset_credits: dict[str, Any] | None = None
    reset_outcome: str | None = None


@dataclass(frozen=True)
class CodexLoginPrompt:
    mode: str
    login_id: str
    auth_url: str | None = None
    verification_url: str | None = None
    user_code: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class _AppServer:
    def __init__(
        self,
        command: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> None:
        self.timeout = timeout
        self.response_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.responses: dict[int, queue.Queue[Any]] = {}
        self.notifications: queue.Queue[Any] = queue.Queue()
        self.next_id = 0
        self.closed = False
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            raise CodexError(f"cannot start Codex App Server: {exc}") from exc
        if (
            self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            self.process.kill()
            raise CodexError("Codex App Server did not expose stdio")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "openswap",
                        "title": "OpenSwap",
                        "version": __version__,
                    }
                },
            )
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        with self.response_lock:
            request_id = self.next_id
            self.next_id += 1
            response: queue.Queue[Any] = queue.Queue(maxsize=1)
            self.responses[request_id] = response
        try:
            payload: dict[str, Any] = {"method": method, "id": request_id}
            if params is not None:
                payload["params"] = params
            self._send(payload)
            try:
                message = response.get(timeout=timeout or self.timeout)
            except queue.Empty as exc:
                raise CodexError("Codex App Server timed out") from exc
            if message is None:
                raise CodexError("Codex App Server stopped early")
            if isinstance(message, BaseException):
                raise CodexError("cannot read Codex App Server output") from message
            if "error" in message:
                error = message["error"]
                text = (
                    error.get("message", "unknown RPC error")
                    if isinstance(error, dict)
                    else str(error)
                )
                raise CodexError(f"Codex App Server: {text}")
            return message.get("result")
        finally:
            with self.response_lock:
                self.responses.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def wait_notification(
        self, method: str, login_id: str, *, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError("Codex login timed out")
            try:
                message = self.notifications.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexError("Codex login timed out") from exc
            if message is None:
                raise CodexError("Codex App Server stopped during login")
            if isinstance(message, BaseException):
                raise CodexError("cannot read Codex login notification") from message
            if message.get("method") != method:
                continue
            params = message.get("params")
            if isinstance(params, dict) and params.get("loginId") == login_id:
                return params

    def close(self) -> None:
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise CodexError("Codex App Server is closed")
        with self.write_lock:
            try:
                self.process.stdin.write(
                    json.dumps(payload, separators=(",", ":")) + "\n"
                )
                self.process.stdin.flush()
            except OSError as exc:
                raise CodexError("cannot write to Codex App Server") from exc

    def _read_stdout(self) -> None:
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and "method" not in message:
                    with self.response_lock:
                        response = self.responses.get(request_id)
                    if response is not None:
                        response.put(message)
                else:
                    self.notifications.put(message)
        except Exception as exc:  # noqa: BLE001 - forward reader failures to caller
            self.notifications.put(exc)
        finally:
            with self.response_lock:
                responses = list(self.responses.values())
            for response in responses:
                try:
                    response.put_nowait(None)
                except queue.Full:
                    pass
            self.notifications.put(None)

    def _read_stderr(self) -> None:
        for _ in self.process.stderr:
            pass


class CodexLoginSession:
    def __init__(
        self,
        server: _AppServer,
        prompt: CodexLoginPrompt,
        expected_redirect: urllib.parse.ParseResult | None,
        expected_state: str | None,
    ) -> None:
        self.server = server
        self.prompt = prompt
        self.expected_redirect = expected_redirect
        self.expected_state = expected_state

    def wait(self, timeout: float = 15 * 60) -> None:
        params = self.server.wait_notification(
            "account/login/completed", self.prompt.login_id, timeout=timeout
        )
        if params.get("success") is True:
            return
        error = params.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = error
        raise CodexError(str(message or "Codex login failed"))

    def cancel(self) -> None:
        try:
            self.server.request(
                "account/login/cancel", {"loginId": self.prompt.login_id}, timeout=10
            )
        finally:
            self.server.close()

    def close(self) -> None:
        self.server.close()

    def forward_callback(self, callback_url: str) -> None:
        if self.prompt.mode != "browser" or self.expected_redirect is None:
            raise CodexError("no browser login is waiting for a callback")
        if len(callback_url) > 8192:
            raise CodexError("callback URL is too long")
        try:
            parsed = urllib.parse.urlparse(callback_url)
            port = parsed.port
        except ValueError as exc:
            raise CodexError("malformed callback URL") from exc
        expected_port = self.expected_redirect.port
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.username is not None
            or parsed.password is not None
            or port != expected_port
            or parsed.path != self.expected_redirect.path
            or parsed.fragment
        ):
            raise CodexError("callback URL does not match the Codex listener")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        if not code or not state:
            raise CodexError("callback URL has no authorization code or state")
        if self.expected_state and not secrets.compare_digest(state, self.expected_state):
            raise CodexError("callback state does not match the pending login")
        local_url = urllib.parse.urlunparse(
            ("http", f"127.0.0.1:{expected_port}", parsed.path, "", parsed.query, "")
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        try:
            with opener.open(local_url, timeout=10) as response:
                response.read(4096)
        except urllib.error.HTTPError as exc:
            if not 300 <= exc.code < 400:
                raise CodexError("Codex callback listener rejected the URL") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CodexError("cannot reach the local Codex callback listener") from exc


class CodexClient:
    OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    CHATGPT_BACKEND_URL = "https://chatgpt.com/backend-api"

    def __init__(self, binary: Path, timeout: float = 45.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def version(self) -> str:
        try:
            result = subprocess.run(
                [str(self.binary), "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexError(f"cannot run Codex: {exc}") from exc
        return result.stdout.strip()

    def login(self, codex_home: Path, *, browser: bool = False) -> None:
        command = self._command("login")
        if not browser:
            command.append("--device-auth")
        env = self._environment(codex_home)
        try:
            result = subprocess.run(command, env=env, check=False)
        except OSError as exc:
            raise CodexError(f"cannot start Codex login: {exc}") from exc
        if result.returncode != 0:
            raise CodexError(f"Codex login exited with status {result.returncode}")

    def logout(self, codex_home: Path) -> None:
        try:
            result = subprocess.run(
                self._command("logout"),
                env=self._environment(codex_home),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexError(f"cannot clear Codex login: {exc}") from exc
        if result.returncode != 0:
            raise CodexError(f"Codex logout exited with status {result.returncode}")

    def refresh_tokens(self, refresh_token: str) -> dict[str, str]:
        """Run Codex's refresh grant as recovery for invalidated access tokens."""
        body = json.dumps(
            {
                "client_id": self.OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.OAUTH_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "openswap/0.1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    detail = error.get("message")
                elif isinstance(error, str):
                    description = payload.get("error_description")
                    detail = f"{error}: {description}" if description else error
                else:
                    detail = None
            except (ValueError, json.JSONDecodeError, AttributeError):
                detail = None
            raise CodexError(detail or f"OAuth refresh failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CodexError("OAuth refresh network error") from exc
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
            raise CodexError("OAuth refresh returned no access token")
        tokens = {"access_token": result["access_token"]}
        for name in ("refresh_token", "id_token"):
            value = result.get(name)
            if isinstance(value, str) and value:
                tokens[name] = value
        return tokens

    def usage(self, access_token: str, account_id: str) -> CodexSnapshot:
        raw = self._chatgpt_request(
            "/wham/usage", access_token, account_id
        )
        rate_limit = raw.get("rate_limit") if isinstance(raw, dict) else None
        limits = self._convert_rate_limit(rate_limit)
        reset_raw = raw.get("rate_limit_reset_credits") if isinstance(raw, dict) else None
        reset_credits = None
        if isinstance(reset_raw, dict):
            count = reset_raw.get("available_count")
            if isinstance(count, (int, float)):
                reset_credits = {"availableCount": max(0, int(count))}
        plan = raw.get("plan_type") if isinstance(raw, dict) else None
        account = {"planType": plan} if isinstance(plan, str) else None
        return CodexSnapshot(
            account=account,
            rate_limits=limits,
            rate_limits_by_id={"codex": limits} if limits is not None else None,
            reset_credits=reset_credits,
        )

    def account_usage(self, codex_home: Path) -> dict[str, Any]:
        server = self._app_server(codex_home)
        try:
            result = server.request("account/usage/read", {})
        finally:
            server.close()
        if not isinstance(result, dict):
            raise CodexError("Codex returned invalid token usage")

        buckets = result.get("dailyUsageBuckets")
        if buckets is None:
            buckets = []
        if not isinstance(buckets, list):
            raise CodexError("Codex returned invalid daily token usage")

        daily: list[dict[str, Any]] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            start_date = bucket.get("startDate")
            tokens = bucket.get("tokens")
            if (
                not isinstance(start_date, str)
                or not isinstance(tokens, int)
                or isinstance(tokens, bool)
                or tokens < 0
            ):
                continue
            try:
                dt.date.fromisoformat(start_date)
            except ValueError:
                continue
            daily.append({"date": start_date, "tokens": tokens})
        daily.sort(key=lambda item: item["date"])

        raw_summary = result.get("summary")
        summary = raw_summary if isinstance(raw_summary, dict) else {}

        def optional_count(name: str) -> int | None:
            value = summary.get(name)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            ):
                return value
            return None

        return {
            "daily": daily,
            "lifetime_tokens": optional_count("lifetimeTokens"),
        }

    def inspect(
        self,
        codex_home: Path,
        *,
        refresh: bool = True,
        consume_reset_key: str | None = None,
    ) -> CodexSnapshot:
        server = self._app_server(codex_home)
        try:
            account_result = server.request(
                "account/read", {"refreshToken": refresh}
            )
            reset_outcome = None
            if consume_reset_key:
                credit_id = self._earliest_reset_credit_id(server)
                params: dict[str, Any] = {
                    "idempotencyKey": consume_reset_key,
                    "creditId": credit_id,
                }
                reset_result = server.request(
                    "account/rateLimitResetCredit/consume", params
                )
                if isinstance(reset_result, dict):
                    value = reset_result.get("outcome")
                    reset_outcome = value if isinstance(value, str) else None
            account = account_result.get("account") if isinstance(account_result, dict) else None
            return CodexSnapshot(
                account=account,
                rate_limits=None,
                reset_outcome=reset_outcome,
            )
        finally:
            server.close()

    @staticmethod
    def _earliest_reset_credit_id(server: _AppServer) -> str:
        result = server.request("account/rateLimits/read", {})
        reset_credits = (
            result.get("rateLimitResetCredits")
            if isinstance(result, dict)
            else None
        )
        credits = (
            reset_credits.get("credits")
            if isinstance(reset_credits, dict)
            else None
        )
        if not isinstance(credits, list):
            raise CodexError("Codex did not return reset-credit details")

        candidates: list[tuple[float, int, str]] = []
        for position, credit in enumerate(credits):
            if not isinstance(credit, dict) or credit.get("status") != "available":
                continue
            credit_id = credit.get("id")
            if not isinstance(credit_id, str) or not credit_id:
                continue
            expires_at = credit.get("expiresAt")
            expiry_order = (
                float(expires_at)
                if isinstance(expires_at, (int, float))
                and not isinstance(expires_at, bool)
                else float("inf")
            )
            candidates.append((expiry_order, position, credit_id))

        if not candidates:
            raise CodexError("Codex returned no selectable reset credit")
        return min(candidates)[2]

    def start_login(self, codex_home: Path, mode: str) -> CodexLoginSession:
        if mode not in {"device", "browser"}:
            raise CodexError(f"unsupported Codex login mode: {mode}")
        server = self._app_server(codex_home)
        try:
            result = server.request(
                "account/login/start",
                {"type": "chatgptDeviceCode" if mode == "device" else "chatgpt"},
            )
            if not isinstance(result, dict) or not isinstance(
                result.get("loginId"), str
            ):
                raise CodexError("Codex returned a malformed login response")
            login_id = result["loginId"]
            if mode == "device":
                verification_url = result.get("verificationUrl")
                user_code = result.get("userCode")
                if not isinstance(verification_url, str) or not isinstance(
                    user_code, str
                ):
                    raise CodexError("Codex returned no device login URL or code")
                prompt = CodexLoginPrompt(
                    mode=mode,
                    login_id=login_id,
                    verification_url=verification_url,
                    user_code=user_code,
                )
                return CodexLoginSession(server, prompt, None, None)

            auth_url = result.get("authUrl")
            if not isinstance(auth_url, str):
                raise CodexError("Codex returned no browser login URL")
            parsed_auth = urllib.parse.urlparse(auth_url)
            auth_query = urllib.parse.parse_qs(parsed_auth.query)
            redirects = auth_query.get("redirect_uri", [])
            states = auth_query.get("state", [])
            if len(redirects) != 1 or len(states) != 1:
                raise CodexError("Codex browser login has no callback metadata")
            expected_redirect = urllib.parse.urlparse(redirects[0])
            if (
                expected_redirect.scheme != "http"
                or expected_redirect.hostname != "localhost"
                or expected_redirect.path != "/auth/callback"
                or expected_redirect.port is None
            ):
                raise CodexError("Codex returned an unexpected callback listener")
            prompt = CodexLoginPrompt(
                mode=mode,
                login_id=login_id,
                auth_url=auth_url,
            )
            return CodexLoginSession(
                server, prompt, expected_redirect, states[0]
            )
        except BaseException:
            server.close()
            raise

    def _app_server(self, codex_home: Path) -> _AppServer:
        return _AppServer(
            self._command("app-server"),
            self._environment(codex_home),
            self.timeout,
        )

    @staticmethod
    def _environment(codex_home: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["NO_COLOR"] = "1"
        return env

    def _command(self, subcommand: str) -> list[str]:
        return [
            str(self.binary),
            "--disable",
            "plugins",
            "--disable",
            "apps",
            subcommand,
        ]

    def _chatgpt_request(
        self,
        path: str,
        access_token: str,
        account_id: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.CHATGPT_BACKEND_URL + path,
            headers={
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
                "Accept": "application/json",
                "User-Agent": "openswap/0.1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise CodexError(f"ChatGPT usage API returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CodexError("ChatGPT usage API network error") from exc
        if not isinstance(result, dict):
            raise CodexError("ChatGPT usage API returned malformed JSON")
        return result

    @staticmethod
    def _convert_rate_limit(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        def window(value: Any) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            used = value.get("used_percent")
            duration = value.get("limit_window_seconds")
            reset = value.get("reset_at")
            if not isinstance(used, (int, float)):
                return None
            result: dict[str, Any] = {"usedPercent": used}
            if isinstance(duration, (int, float)):
                result["windowDurationMins"] = round(duration / 60)
            if isinstance(reset, (int, float)):
                result["resetsAt"] = reset
            return result

        return {
            "limitId": "codex",
            "primary": window(raw.get("primary_window")),
            "secondary": window(raw.get("secondary_window")),
        }
