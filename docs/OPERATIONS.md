# Operations

OpenSwap has one operational model on Windows and Linux:

1. install the Python package;
2. edit `config.toml` manually;
3. start one process;
4. perform every management action in Telegram.

There are no setup, status, repair, or routing commands.

## Installation

Requirements:

- Python 3.11 or newer;
- the official Codex CLI in `PATH`, or its absolute executable path;
- OpenCode or an `auth.json`-compatible client;
- optional OpenSSH and Python on remote hosts for multihost operation.

Create a virtual environment:

```bash
python -m venv .venv
```

Linux installation:

```bash
.venv/bin/python -m pip install -e .
cp config.example.toml config.toml
```

Windows PowerShell installation:

```powershell
.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.toml config.toml
```

The package has one runtime dependency, `portalocker`, for cross-platform
process locks.

## Configuration lifecycle

OpenSwap looks for `config.toml` in the launch directory. Both included start
scripts switch to the repository directory before starting Python.

The configuration is strict. A misspelled key is an error rather than a silently
ignored setting. On POSIX systems OpenSwap changes the file mode to `0600`
because it contains the Telegram token.

All relative local paths are resolved from `config.toml`:

```toml
[storage]
directory = "./data"
```

This remains stable regardless of the shell or service-manager working
directory.

Edit the file while OpenSwap is stopped, then restart. Runtime reload is not
supported by design.

## Required settings

Telegram:

```toml
[telegram]
token = "123456:replace-me"
allowed_users = [123456789]
```

Only these numeric user IDs may interact with the bot, and only in private
chats.

Storage and OpenCode:

```toml
[storage]
directory = "./data"

[opencode]
auth_file = "/home/user/.local/share/opencode/auth.json"
```

Codex:

```toml
[codex]
binary = "codex"
```

An absolute path is also accepted. OpenSwap checks the Codex version in the
System screen but does not install or update the executable.

## Starting and stopping

Linux:

```bash
./start.sh
```

Windows PowerShell:

```powershell
.\start.ps1
```

Both launchers execute the same Python module and accept no arguments. Stop the
foreground process with `Ctrl+C`.

For unattended operation, point the operating system's normal user-level
autostart facility at the corresponding script. OpenSwap intentionally does not
install or maintain systemd units, Windows services, scheduled tasks, or Docker
containers.

Startup errors are written to the console with the configuration file and
setting involved. Once Telegram polling starts, scheduler and network warnings
continue in the same output.

## Telegram operation

`/start` creates or replaces the single OpenSwap menu. Session rows open account
details and actions.

Session buttons never include host counts. In a true multihost, multi-Session
deployment, the root text contains a `Host assignments` block listing only
Sessions with one or more assigned hosts. The block is intentionally absent for
one Session or one host.

The bot supports:

- official Codex device-code and browser login;
- bounded `auth.json` upload;
- Session refresh and reauthorization;
- default Session selection;
- per-host assignment and return to default;
- confirmed removal of unused Sessions;
- confirmed earned reset redemption;
- usage refresh for one or every Session;
- combined and per-Session token activity for 7 days, 30 days, and lifetime;
- English and Russian interfaces;
- System health and synchronization retry.

The System button is always on the root screen. With several hosts it is on the
left and Hosts is on the right of the same row. In single-host mode System
occupies the whole row.

The screen reports OpenCode validity, Codex availability, storage integrity,
healthy Sessions, and target convergence. `Retry sync` wakes the scheduler and
returns immediately.

Usage, reset windows, and earned reset credits refresh automatically for every
healthy Session on the configured scheduler cadence. No manual Refresh is
required. A transient error on one Session leaves its previous snapshot visible
with a stale marker while other Sessions and Telegram menus continue updating.

Account-wide token history refreshes independently every 30 minutes through the
official Codex App Server. The Token activity screen shows total and per-Session
7-day, 30-day, and lifetime values in one shared unit, repeats `Σ Total`, and
labels partial account coverage explicitly. The backend account profile already
includes all Codex apps and configured hosts for that ChatGPT account; host
assignments are not multiplied. Root and Session Refresh force both allowance
and token refresh. Collection is sequential and does not hold the registry lock
while Codex runs.

## Multihost configuration

The local target must match `[opencode].auth_file` exactly:

```toml
[sync]
interval_seconds = 120
connect_timeout_seconds = 5
command_timeout_seconds = 15

[[hosts]]
name = "local"
auth_file = "/home/user/.local/share/opencode/auth.json"

[[hosts]]
name = "server"
auth_file = "/home/user/.local/share/opencode/auth.json"
ssh = "user@server.example"
python = "python3"
```

Remote requirements:

- non-interactive OpenSSH access;
- a Python command declared by `python`;
- permission to read and replace the configured file;
- no interactive shell setup required by the SSH session.

Use `python = "python"` for a Windows remote when appropriate. Remote paths are
interpreted by that remote Python process, not the coordinator.

Target state in Telegram:

- `synced` — assigned credentials match;
- `applying` — a new assignment is queued;
- `busy · retry scheduled` — compare-and-swap detected another writer;
- `offline` — SSH timed out or was unavailable;
- `error` — configuration, permissions, Python, or document shape needs work.

Offline assignments remain in the registry and converge when the host returns.

## Backup and restore

Stop OpenSwap before copying or restoring state. Back up both:

```text
config.toml
data/
```

Treat the backup as a secret. `config.toml` contains the Telegram token, and
`data/accounts/*/auth.json` contains live OAuth credentials.

The important state files are:

```text
data/registry.json
data/accounts/<Session UUID>/auth.json
```

After restoration, start OpenSwap and use System → Retry sync. The canonical
Session slots rebuild the local and reachable remote OpenCode views.

## Troubleshooting

### The process exits before Telegram starts

Read the console error. Common causes are a missing table, unknown key, invalid
user ID, relative remote path, duplicate host name, or a local host that does
not match `[opencode].auth_file`.

### Codex is unavailable

Run `codex --version` in the same user context as OpenSwap. If it is not in
`PATH`, place its absolute executable path in `[codex].binary`, restart, and
open System again.

### OpenCode auth is missing

The bot can create the OpenAI entry when the first Session becomes active. If
the configured path is wrong, correct it and restart rather than creating a
second configuration.

### A Session requires login

Open the Session in Telegram and choose the login action. Its alias and routing
assignment remain intact.

### Usage is marked stale

Open System to identify whether one or several healthy Sessions missed their
last usage refresh. The previous snapshot remains visible. Refresh is retried on
the next scheduled tick; other Sessions continue updating independently.

### Token activity is missing or stale

Open Token activity and press Refresh. A Session requiring login cannot refresh
its account history and remains outside the available-account total until
reauthorized. A transient Codex error keeps the previous token cache visible
with a stale marker and is retried after the independent 30-minute cache window.

### A host remains offline

Verify non-interactive SSH and the configured remote Python command outside
OpenSwap. Correct `config.toml`, restart, and use System → Retry sync.

### A host is busy

OpenCode changed the target during publication. The compare-and-swap guard
prevented an overwrite. Allow the configured interval to retry, or stop the
competing writer briefly and press Retry sync.

### Telegram does not respond

Verify the token, numeric allowlist, private-chat context, and console network
errors. Configuration changes require restart.

## Upgrade

Stop OpenSwap, update the source, refresh the virtual environment, review any
documented configuration change, and restart:

```bash
git pull
.venv/bin/python -m pip install -e .
./start.sh
```

On Windows, use the equivalent `.venv\Scripts\python.exe` and `start.ps1`.
Never overwrite `config.toml` or `data/` during an upgrade.
