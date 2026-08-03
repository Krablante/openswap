<div align="center">

# OpenSwap

**A small Telegram control plane for ChatGPT Sessions in OpenCode and Codex CLI.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Windows and Linux](https://img.shields.io/badge/Windows%20%7C%20Linux-supported-2ea44f)](#windows-and-linux)
[![OpenCode](https://img.shields.io/badge/OpenCode-compatible-111111)](https://opencode.ai/)
[![Codex CLI](https://img.shields.io/badge/Codex%20CLI-compatible-111111)](https://developers.openai.com/codex/cli)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)

</div>

OpenCode normally has one active OpenAI login. That becomes awkward when you
have several ChatGPT accounts, work from more than one machine, or want to know
which usage window resets first. Switching credentials by hand is easy to get
wrong, and it gives you no useful view of account health or remaining usage.

OpenSwap turns those credentials into named Sessions shared by two independent
workspaces: OpenCode-compatible clients and Codex CLI. Each workspace keeps its
own active/default Session, so OpenCode can use Session 3 while Codex uses
Session 2. OpenSwap puts the entire workflow in one compact Telegram message.
It shows live allowance, reset windows, account-wide token history, login
health, earned reset credits, and
per-host routing. Its target-specific merge preserves every field it does not
own.

The OpenCode workspace also works with
[OpenCodez](https://github.com/Krablante/opencodez), which uses the same
compatible credential store.

## One interface on purpose

OpenSwap has no management CLI, setup wizard, web panel, database, or Docker
stack. There is one manually edited `config.toml`, one process, and one operator
interface: Telegram.

That means every capability lives in one place:

- add a Session with official Codex device-code or browser OAuth;
- import an existing Codex CLI, OpenCode, or OpenCodez `auth.json`;
- export a Session as Codex CLI or OpenCode/OpenCodez `auth.json`;
- inspect plan, allowance, reset times, token history, and login health;
- refresh or reauthorize a Session;
- select a different default Session for OpenCode and Codex CLI;
- assign a different Session to one host;
- consume the nearest expiring earned reset after confirmation;
- remove an unassigned Session;
- switch between English and Russian;
- inspect system health and retry synchronization.

The bot edits one message instead of filling the chat. Credential uploads,
OAuth callbacks, and `/language` command messages remove themselves after use.

## Quick start

Install Python 3.11 or newer and the official
[Codex CLI](https://developers.openai.com/codex/cli). The `codex` executable
must be available in `PATH`, or configured with an absolute path.

Clone OpenSwap and create one virtual environment:

```bash
git clone https://github.com/Krablante/openswap.git
cd openswap
python -m venv .venv
```

On Linux:

```bash
.venv/bin/python -m pip install -e .
cp config.example.toml config.toml
```

On Windows PowerShell:

```powershell
.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.toml config.toml
```

Edit `config.toml`, then start the bot:

```bash
./start.sh
```

```powershell
.\start.ps1
```

OpenSwap reads the configuration once at startup. Edit the file and restart the
process to apply a change. There is no watcher, hidden generated configuration,
or second source of truth.

## Configuration

The minimal single-host configuration is deliberately small:

```toml
[telegram]
token = "123456:replace-me"
allowed_users = [123456789]

[storage]
directory = "./data"

[opencode]
auth_file = "/home/user/.local/share/opencode/auth.json"

[codex]
binary = "codex"
auth_file = "~/.codex/auth.json"
```

`codex.auth_file` is optional. When it is present, the workspace switch appears
in Telegram and OpenSwap manages the file-based Codex login. Codex must use
`cli_auth_credentials_store = "file"`; OS keychain credentials cannot be
switched by replacing `auth.json`. Omit `auth_file` to retain the original
OpenCode-only interface and behavior.

All relative paths are resolved from `config.toml`, never from an unpredictable
shell working directory. On Windows, TOML literal strings avoid escaping
backslashes:

```toml
[opencode]
auth_file = 'C:\path\to\opencode\auth.json'
```

The configuration file contains the Telegram token. OpenSwap automatically
restricts it to the current user on POSIX systems. On Windows, keep it inside a
directory owned by your user account. `config.toml` and `data/` are ignored by
Git.

See [`config.example.toml`](config.example.toml) for scheduler and multihost
settings.

## The Telegram menu

Every screen identifies the selected workspace with `🔵 opencode` or `🟣 codex`.
The root ends with one `⇄ Codex` or `⇄ OpenCode` button that redraws the same
compact interface for the other client. The Session pool, allowance, token
activity, login, import, export, refresh, and reset actions are shared. Active
markers, default selection, Hosts, and target health apply only to the selected
workspace. The switch is hidden during sign-in and import prompts.

The root screen shows Sessions as compact buttons with remaining allowance,
reset countdown, and earned reset credits. Selecting a Session opens its
details and actions. Host distribution never appears inside a Session button;
with several Sessions and several hosts it is rendered as a separate
`🖥 Host assignments` text block. The block is omitted for one Session or one
host because it would only repeat the active/default state.

Session numbers are compact display labels, not identities. Deleting a Session
immediately renumbers the remaining labels without gaps (`1, 3` becomes `1,
2`). Stable UUIDs remain unchanged, so default routing, host overrides, menu
callbacks, and isolated credential slots continue to reference the same
accounts.

The root also shows the combined rolling 7-day and 30-day token activity.
`Token activity` opens a comparison screen with 7-day, 30-day, and lifetime
totals across all available Sessions and the same three values for every
Session. The screen uses one natural shared unit (`B` for billion-scale totals)
and repeats an explicit approximate `Σ Total`, so the displayed rounded rows
can be added directly. Missing Sessions remain visible and partial totals are
labeled with the available account count. Raw calculations remain exact; `≈`
marks the compact presentation.

The source is account-wide: each Session represents one ChatGPT account, and
its values already include every Codex app or device using that account,
including configured hosts. Host assignments are therefore never multiplied.
The values are backend-reported token activity, not API billing, monetary cost,
remaining allowance, or host-level attribution.

`System` is always available without taking over the interface:

- with several hosts, `System` is on the left and `Hosts` is on the right of the
  same navigation row;
- with zero or one configured host, `System` occupies the full navigation row and
  host-management controls disappear.

The System screen reports:

```text
OpenSwap 2.4.0

✓ 🔐 Codex CLI · ready
✓ 🤖 Codex · codex-cli 0.x.x
✓ 💾 Storage
! 👤 Sessions · 2/3
✓ 📊 Usage freshness · 2/2
✓ 🧮 Token activity · 2/2
✓ 🌐 Hosts · 5/5

Attention
• One or more Sessions require login
```

It also provides `Retry sync` and `Refresh`. Retrying only wakes the existing
scheduler; Telegram never waits for SSH.

Every scheduler tick refreshes usage and earned reset data for every healthy
Session whose snapshot is due. OAuth refresh no longer suppresses usage refresh,
and a failure in one Session cannot block another Session or the Telegram menu.
Stale data is marked directly in the Session row and in System.

Managed Business workspaces can return no ordinary rate-limit window because
they use monthly workspace credits instead. OpenSwap reads the official
`spend_control.individual_limit`: Telegram shows remaining percentage, credits
used versus limit, and monthly reset time. When exhausted it shows `Workspace
limit reached`; other explicit states include unlimited workspace allowance and
a genuinely unavailable allowance window.

For managed-workspace sign-in, Device Code may require an administrator to
enable device-code authentication. Browser sign-in supports SSO but still
depends on its callback completing. If either method fails, Telegram returns to
the sign-in choices and offers the official transferable `auth.json` fallback.

Token history uses the official Codex App Server `account/usage/read` method.
It is cached independently for 30 minutes because weekly and monthly aggregates
do not justify starting a Codex subprocess every minute. Manual Refresh bypasses
that cache. Collection is sequential and performed outside the registry lock,
keeping transient CPU use low and Telegram actions responsive.

## More than one host

Add a local host and any SSH targets to the same file:

```toml
[sync]
interval_seconds = 120
connect_timeout_seconds = 5
command_timeout_seconds = 15

[[hosts]]
name = "local"
auth_file = "/home/user/.local/share/opencode/auth.json"
codex_auth_file = "~/.codex/auth.json"

[[hosts]]
name = "server"
auth_file = "/home/user/.local/share/opencode/auth.json"
codex_auth_file = "~/.codex/auth.json"
ssh = "user@server.example"
python = "python3"
```

Exactly one local host must point at `[opencode].auth_file`. When the Codex
workspace is enabled, exactly one local `codex_auth_file` must also match
`[codex].auth_file`; remote `codex_auth_file` values remain optional. Remote
paths remain native to the remote operating system, so a Linux coordinator can
target a Windows path and a Windows coordinator can target Linux. For a Windows
SSH target, set `python = "python"` when that is its Python command.

Routing stays intentionally sparse: each workspace has one default Session, and
only its hosts that differ are stored as overrides. New hosts inherit that
workspace's default. Offline assignments are retained. Successful local changes
become `synced` in the same
transaction; remote writes use bounded SSH and compare-and-swap.

## Windows and Linux

The application code is shared. Platform differences are isolated to one small
storage module:

- `portalocker` provides the same process lock on Windows and Linux;
- `os.replace` publishes files atomically;
- Windows sharing violations receive a short bounded retry;
- POSIX permissions are enforced only where they have real meaning;
- no Unix module is imported on Windows;
- SSH is optional and needed only for remote hosts.

The launch scripts differ only in how they locate the virtual-environment
Python. There are no separate Windows and Linux editions.

## Safety

Each Session lives in an isolated credential slot. OpenCode publication replaces
only `openai`; Codex publication replaces only `auth_mode`, `OPENAI_API_KEY`,
`tokens`, and `last_refresh`. Unknown top-level keys survive in both live files.
The registry stores routing, fingerprints, health, and usage metadata, but never
raw access or refresh tokens.
OpenSwap rejects dead imports, blocks deletion of assigned Sessions, preserves
unknown providers, uses atomic writes, and refuses a remote update when the file
changed during publication.

Telegram accepts commands only from configured numeric user IDs in private
chats. Unauthorized updates receive no response. A Session export is available
only from its detail screen, requires an explicit format choice, and sends the
credential document directly from memory. The bot cannot run arbitrary commands
or read arbitrary paths.

Stop the process before backing up or restoring `data/`. Live OpenCode and Codex
`auth.json` files are published views; the isolated Session slots are canonical.

See [Architecture](docs/ARCHITECTURE.md),
[Operations](docs/OPERATIONS.md), and [Security](SECURITY.md) for the complete
contracts.

## License

[MIT](LICENSE)
