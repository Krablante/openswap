# Architecture

OpenSwap is a Telegram-oriented control plane for ChatGPT Sessions used by
OpenCode-compatible clients. It deliberately avoids a management CLI, web
server, database, configuration generator, and container runtime.

The complete runtime is one Python process:

```text
config.toml
    │
    ▼
OpenSwap daemon
    ├── Telegram long poll
    ├── scheduler
    ├── Session registry and credential slots
    ├── Codex App Server subprocesses
    └── optional bounded SSH reconciliation
```

## Configuration boundary

`config.toml` is the only operator configuration. It is parsed strictly once at
startup. Unknown keys, invalid types, duplicate host names, relative remote
paths, unsafe SSH destinations, and ambiguous local targets stop startup with a
specific error.

Relative local paths are resolved from the configuration file. Remote paths are
kept as remote-native strings and never interpreted by the coordinator's
operating system. Exactly one host in a multihost configuration must be local
and use the same file as `[opencode].auth_file`.

The configuration is not watched. Restarting is the explicit and only way to
apply a topology or path change. This keeps runtime state deterministic and
eliminates partial reloads.

## State and credentials

The configured storage directory contains:

```text
registry.json
accounts/<Session UUID>/auth.json
accounts/<Session UUID>/codex-home/
openswap.lock
sync.lock
```

`registry.json` uses schema 2. It stores Session aliases, safe identity
metadata, usage snapshots, fingerprints, timestamps, Telegram menu state, one
default Session UUID, and sparse host overrides. It never stores raw access or
refresh tokens.

Each account slot contains the canonical credential document for one ChatGPT
account. The live OpenCode `auth.json` and remote target files are published
views. Only their `openai` entry may be replaced; unrelated providers survive
unchanged.

On POSIX systems, state directories are `0700` and credential/config files are
`0600`. Windows relies on the current user's directory ACL because POSIX mode
bits do not express Windows access control.

## Routing model

Routing is intentionally represented as:

```json
{
  "default_account": "session-uuid",
  "target_overrides": {
    "server": "another-session-uuid"
  }
}
```

The effective Session is one lookup:

```python
assigned = target_overrides.get(target_name, default_account)
```

This preserves the simple “one Session everywhere” behavior while allowing
explicit exceptions. A new host inherits the default automatically. Selecting
the default for a host removes its redundant override. A Session cannot be
deleted while it is the default or assigned to any host.

With one configured host, Telegram hides host navigation and uses the effective
target assignment as the active Session. Selecting a Session updates the
default and clears stale overrides atomically. With several hosts, the
`Hosts → host → Session` drill-down exposes overrides directly.

## Telegram UI

Telegram is the only management interface. One editable message represents the
current view. `/start` replaces an old menu; `/language` opens the language
selector and removes its command message.

The root contains Session rows, `Add`, `Refresh`, and the bottom navigation:

- multihost: `System` is left and `Hosts` is right on one row;
- single-host: `System` occupies the full row.

Session buttons contain only Session identity, usage/reset countdown, stale
state, and earned reset credits. Routing counts belong to a separate
`Host assignments` text block, rendered only when both the Session count and
host count exceed one. Dead assigned Sessions remain visible there. A one-
Session or one-host deployment suppresses the block rather than repeating an
obvious assignment.

The System view is computed only when opened or refreshed. It checks the
configured OpenCode document, Codex version, registry and account slots,
Session health, SSH availability, and current convergence. It exposes no paths,
tokens, account IDs, or SSH destinations.

`Retry sync` marks reconciliation dirty and wakes the scheduler through an
in-process event. The callback returns immediately; no Telegram request waits
for local or remote I/O.

English is implicit. Only explicit Russian preferences are stored, keyed by an
allowlisted Telegram user ID. Command descriptions are installed in both
languages and then scoped to the selected chat language.

## Scheduler

The main thread owns a bounded scheduler loop. The Telegram long poll runs in
one daemon thread. Login flows use short-lived worker threads because official
Codex authorization can wait for human input.

Each scheduler pass performs:

1. due OAuth and usage refresh for every healthy Session;
2. target reconciliation when due or explicitly woken;
3. in-place Telegram menu updates.

Usage runs before SSH on scheduled ticks, so several offline hosts cannot delay
the allowance/reset snapshot by the sum of their connection timeouts. An event
wake between scheduled ticks performs reconciliation without an extra usage
refresh.

These stages are failure-isolated. A sync error cannot suppress usage refresh,
one Session usage error cannot suppress another Session, and a refresh error
cannot suppress Telegram rendering. A missing menu is removed from persisted
menu state without blocking updates for other users.

The scheduler normally sleeps on an event, not a polling loop. Routing changes,
credential refreshes, and `Retry sync` wake it immediately. Otherwise the
configured interval bounds background work.

On each scheduled UI tick, every healthy Session whose usage snapshot has
reached the configured interval is refreshed. A five-second tolerance accounts
for the duration of the previous API pass without increasing the rate above one
refresh per tick. Token refresh and usage refresh share the same account pass,
so token rotation never skips limits and never causes a duplicate limits call.
Per-Session refresh failures are retained as safe metadata and surfaced as
stale usage in Telegram System and Session views.

Credentials are read once per unique assigned Session during a reconciliation
pass, not once per host. Targets are rewritten only when the assigned OpenAI
fingerprint differs.

## Local publication

Local publication performs:

1. a bounded read of the current `auth.json`;
2. replacement of only the `openai` entry;
3. JSON serialization to a temporary file in the same directory;
4. file flush and `fsync`;
5. one final compare-and-swap digest check;
6. atomic `os.replace`;
7. directory `fsync` where the operating system supports it;
8. post-write account verification.

Windows sharing violations receive a fixed bounded retry. There is no unbounded
sleep or retry worker. A successful local route change and its `synced` state
are committed in the same registry transaction.

## Remote publication

SSH is optional. Each remote target declares a destination, native path, and
Python command. OpenSwap invokes:

```text
ssh ... destination python -
```

The bounded Python program is sent through stdin. Paths and credentials are
embedded as Python literals/base64 data rather than shell arguments, avoiding
POSIX-versus-Windows quoting branches. The remote program applies the same
size, JSON, compare-and-swap, flush, atomic replacement, and platform-specific
permission rules as local storage.

Exit code `75` means the file changed during publication. OpenSwap records
`busy · retry scheduled` and waits for the normal sync interval rather than
creating a one-second retry loop. SSH timeout or exit `255` is `offline`.
Assignments remain declarative and converge when the host returns.

## Cross-platform storage boundary

All operating-system-specific behavior is isolated in `storage.py`:

- `portalocker` process locks;
- secure POSIX modes;
- Windows-safe no-op mode checks;
- atomic temporary-file replacement;
- bounded Windows sharing retry;
- directory durability where `O_DIRECTORY` exists.

The core, Telegram, configuration, OAuth, routing, and usage logic contain no
Windows or POSIX branch. This keeps platform maintenance small and testable.

## Codex boundary

OpenSwap delegates ChatGPT authentication, token refresh, usage APIs, and earned
reset operations to the official Codex App Server. The executable is configured
as a command in `PATH` or an absolute path. OpenSwap does not install, update, or
silently replace it.

Each Session receives an isolated Codex home. Login imports only canonical
Codex fields or a compatible OpenCode `openai` OAuth entry. Credentials are
validated against ChatGPT account identity before entering an account slot.

Refresh uses the same isolation and accepts a new token generation only when
its account ID matches the slot. A rejected refresh marks the Session as
requiring login without changing aliases or routing.

## Earned resets

Usage snapshots may include earned reset credits. Telegram shows the available
balance and requires a separate, expiring confirmation before redemption.
OpenSwap always chooses the reset window with the nearest expiry, delegates the
operation to Codex, refreshes usage, and reports the normalized outcome.

Reset credits are never consumed automatically.

## Concurrency and failure model

`portalocker` serializes registry mutations across processes. A separate sync
lock prevents overlapping reconciliation passes. In-process locks serialize
Telegram menu and login state.

The important failure rules are:

- malformed or oversized credential documents are rejected;
- unknown provider entries are preserved;
- account identity mismatch never changes a slot;
- a changed target is never overwritten blindly;
- one offline host does not block other targets;
- desired offline assignments are retained;
- Telegram callbacks remain bounded and never perform SSH;
- configuration changes require restart rather than partial reload;
- canonical slots can rebuild every published target.
