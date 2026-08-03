from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from openswap.core import (
    OpenSwap,
    Settings,
    WorkspaceSnapshot,
    fingerprint,
    read_json,
)
from openswap.storage import atomic_json
from openswap.sync import SyncConfig, SyncTarget, Workspace
from openswap.telegram import TelegramBot, _host_assignment_line


def _jwt(account_id: str) -> str:
    payload = json.dumps(
        {
            "chatgpt_account_id": account_id,
            "exp": int(time.time()) + 3600,
        },
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"e30.{encoded}.signature"


def _auth(account_id: str) -> dict[str, object]:
    access = _jwt(account_id)
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": access,
            "access_token": access,
            "refresh_token": f"refresh-{account_id}",
            "account_id": account_id,
        },
        "last_refresh": "2026-01-01T00:00:00Z",
    }


class WorkspaceSwitchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.opencode_auth = root / "opencode-auth.json"
        self.codex_auth = root / "codex-auth.json"
        self.openswap = OpenSwap(
            Settings(
                state_dir=root / "state",
                target_auth=self.opencode_auth,
                codex_auth=self.codex_auth,
                codex_bin=Path("codex"),
                sync=SyncConfig(
                    interval_seconds=120,
                    connect_timeout_seconds=5,
                    command_timeout_seconds=15,
                    targets=(
                        SyncTarget(
                            host="local",
                            workspace=Workspace.OPENCODE,
                            path=self.opencode_auth,
                        ),
                        SyncTarget(
                            host="local",
                            workspace=Workspace.CODEX,
                            path=self.codex_auth,
                        ),
                    ),
                ),
            )
        )
        self.openswap.initialize()
        self.session_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.account_ids = ["account-one", "account-two"]

        registry = self.openswap._empty_registry()
        for sequence, (session_id, account_id) in enumerate(
            zip(self.session_ids, self.account_ids, strict=True), start=1
        ):
            codex_home = self.openswap._codex_home(session_id)
            self.openswap._prepare_codex_home(codex_home)
            atomic_json(codex_home / "auth.json", _auth(account_id))
            registry["accounts"][session_id] = {
                "id": session_id,
                "sequence": sequence,
                "alias": f"session-{sequence}",
                "name": f"Session {sequence}",
                "created_at": f"2026-01-0{sequence}T00:00:00Z",
                "account_id_fingerprint": fingerprint(account_id)[:16],
                "last_error": None,
            }
        registry["defaults"] = {
            "opencode": self.session_ids[0],
            "codex": self.session_ids[0],
        }
        self.openswap._save_registry(registry)

        first_entry = self.openswap._read_slot_entry(self.session_ids[0])
        atomic_json(
            self.opencode_auth,
            {"openai": first_entry, "unmanaged_provider": {"keep": True}},
        )
        atomic_json(
            self.codex_auth,
            {**_auth(self.account_ids[0]), "unmanaged_field": {"keep": True}},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_switch_converts_one_shared_slot_without_network_refresh(self) -> None:
        with mock.patch.object(
            self.openswap,
            "_inspect_slot",
            side_effect=AssertionError("switch must not inspect or refresh"),
        ):
            self.openswap.use(
                self.session_ids[1], space="codex", all_targets=True
            )

        registry = read_json(self.openswap.registry_path)
        codex = read_json(self.codex_auth)
        opencode = read_json(self.opencode_auth)
        self.assertEqual(registry["defaults"]["codex"], self.session_ids[1])
        self.assertEqual(registry["defaults"]["opencode"], self.session_ids[0])
        self.assertEqual(codex["tokens"]["account_id"], self.account_ids[1])
        self.assertEqual(opencode["openai"]["accountId"], self.account_ids[0])
        self.assertEqual(codex["unmanaged_field"], {"keep": True})

        self.openswap.use(self.session_ids[1], space="opencode")
        opencode = read_json(self.opencode_auth)
        self.assertEqual(opencode["openai"]["accountId"], self.account_ids[1])
        self.assertEqual(opencode["unmanaged_provider"], {"keep": True})

    def test_root_session_row_remains_one_full_width_details_button(self) -> None:
        accounts = [
            {"id": session_id, "name": f"Session {index}", "last_error": None}
            for index, session_id in enumerate(self.session_ids, start=1)
        ]
        snapshot = WorkspaceSnapshot(
            workspace=Workspace.CODEX,
            accounts=tuple(accounts),
            default_account=self.session_ids[0],
            sync={
                "enabled": True,
                "total": 1,
                "targets": [{"account_id": self.session_ids[0]}],
            },
            host_assignments={self.session_ids[0]: ("local",)},
        )
        keyboard = TelegramBot._menu_keyboard(
            snapshot,
            None,
            None,
            None,
            False,
            False,
            None,
            "en",
        )
        self.assertEqual(keyboard[1][0]["callback_data"], f"open:{self.session_ids[1]}")
        self.assertEqual(len(keyboard[1]), 1)

        details = TelegramBot._menu_keyboard(
            snapshot,
            None,
            None,
            None,
            False,
            False,
            self.session_ids[1],
            "en",
        )
        actions = [button["callback_data"] for row in details for button in row]
        self.assertIn(f"use:{self.session_ids[1]}", actions)

    def test_host_assignments_include_names_in_parentheses(self) -> None:
        assignments = {
            self.session_ids[0]: ("nuc", "ser"),
            self.session_ids[1]: ("rtx",),
        }
        self.assertEqual(assignments[self.session_ids[0]], ("nuc", "ser"))
        self.assertEqual(
            _host_assignment_line(
                {"name": "Session 1"}, assignments[self.session_ids[0]], "en"
            ),
            "• 👤 Session 1 · 2 hosts (nuc, ser)",
        )
        self.assertEqual(
            _host_assignment_line(
                {"name": "Session 1"}, assignments[self.session_ids[0]], "ru"
            ),
            "• 👤 Session 1 · 2 хоста (nuc, ser)",
        )


if __name__ == "__main__":
    unittest.main()
