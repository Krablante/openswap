# Architecture

OpenSwap is a Telegram-oriented control plane for one pool of ChatGPT Sessions
used independently by OpenCode-compatible clients and Codex CLI. It deliberately
avoids a management CLI, web server, database, configuration generator, and
container runtime.

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
and use the same file as `[opencode].auth_file`. If `[codex].auth_file` is set,
exactly one local `codex_auth_file` must match it. Remote Codex targets are
optional.

The configuration is not watched. Restarting is the explicit and only way to
apply a topology or path change. This keeps runtime state deterministic and
eliminates partial reloads.

## State and credentials

The configured storage directory contains:

```text
registry.json
accounts/<Session UUID>/codex-home/auth.json
accounts/<Session UUID>/codex-home/config.toml
openswap.lock
sync.lock
```

`registry.json` uses schema 3. It stores Session aliases, safe identity
metadata, allowance snapshots, daily token aggregates, fingerprints,
timestamps, Telegram menu state including the selected workspace, one default
Session UUID per workspace, and sparse host overrides. It never stores raw
access or refresh tokens, prompts, completions,
or per-request traces.

Session UUIDs are stable identities used by default routing, sparse target
overrides, Telegram callbacks, and account directories. `Session N`,
`session-N`, and `sequence` are compact presentation labels. Registry
normalization assigns them chronologically as `1..N` after deletion and sets
`next_sequence=N+1`; it never rewrites UUID-based relationships.

Each account slot contains the canonical credential document for one ChatGPT
account. Live OpenCode and Codex `auth.json` files are published views. OpenCode
publication replaces only `openai`. Codex publication replaces only
`auth_mode`, `OPENAI_API_KEY`, `tokens`, and `last_refresh`; other top-level
state survives unchanged.

Telegram can export a healthy Session in either canonical Codex CLI shape or a
standalone OpenCode/OpenCodez document containing only the `openai` entry. The
Session is verified and refreshed through the normal Codex path first. Export
serialization and Telegram multipart upload stay in memory; no export file is
written to storage.

On POSIX systems, state directories are `0700` and credential/config files are
`0600`. Windows relies on the current user's directory ACL because POSIX mode
bits do not express Windows access control.

## Routing model

Routing is intentionally represented as:

```json
{
  "defaults": {
    "opencode": "session-uuid-3",
    "codex": "session-uuid-2"
  },
  "target_overrides": {
    "server": "another-session-uuid",
    "server.codex": "session-uuid-1"
  }
}
```

The effective Session is one lookup:

```python
assigned = target_overrides.get(target_name, defaults[target_kind])
```

This preserves the simple “one Session everywhere in this client” behavior
while allowing explicit exceptions. OpenCode and Codex defaults never affect
each other. A new host inherits its workspace default automatically. Selecting
the default for a host removes its redundant override. A Session cannot be
deleted while it is a default or assigned in either workspace.

Schema 2 migrates in memory on first load: its `default_account` becomes both
`defaults.opencode` and `defaults.codex`, so enabling Codex initially mirrors the
existing choice without changing OpenCode routing.

With one configured host, Telegram hides host navigation and uses the effective
target assignment as the active Session. Selecting a Session updates the
default and clears stale overrides atomically. With several hosts, the
`Hosts → host → Session` drill-down exposes overrides directly.

## Telegram UI

Telegram is the only management interface. One editable message represents the
current view. `/start` replaces an old menu; `/language` opens the language
selector and removes its command message.

The root contains Session rows, a compact combined token summary, `Add`,
`Refresh`, and the bottom navigation:

- `Token activity` occupies a separate full-width row when Sessions exist;
- multihost: `System` is left and `Hosts` is right on the navigation row;
- single-host: `System` occupies the full navigation row.

When `[codex].auth_file` is configured, one final workspace button switches the
root between `🔵 opencode` and `🟣 codex`. The same account-level views are
reused. Active markers, Hosts, default actions, and System target health are
computed for the selected workspace only. Pending login/import flows hide the
switch so their state cannot be confused with routing.

Session buttons contain only Session identity, usage/reset countdown, stale
state, and earned reset credits. The `Hosts → host` picker reuses the same
compact button labels, appending `· default` to the Session inherited from the
default. Routing counts belong to a separate `Host assignments` text block,
rendered only when both the Session count and host count exceed one. Dead
assigned Sessions remain visible there. A one-Session or one-host deployment
suppresses the block rather than repeating an obvious assignment.

The Token activity view computes rolling 7-day, rolling 30-day, and lifetime
totals from cached daily buckets. It shows the aggregate first and one compact
comparison row per Session. Every value on the screen uses one natural shared
unit: billion-scale totals use `B`, million-scale totals use `M`, and so on.
Values are rounded to three decimals for display. The approximate `Σ Total`
adds those displayed rounded rows, so visible arithmetic always remains
checkable while raw registry calculations remain exact. Missing Sessions are
never silently treated as zero.

Each Session is one unique ChatGPT account. `account/usage/read` is account-wide
and already includes every Codex app or device using that account, including
configured hosts. Host assignments affect credential routing only: they are not
multiplied into token activity. Counts are not monetary spend, API billing,
remaining allowance, or host-level attribution.

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
2. due account-wide token history refresh;
3. target reconciliation when due or explicitly woken;
4. in-place Telegram menu updates.

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

The allowance response is also normalized into a small `limit_status` object.
This preserves managed-workspace spend-control exhaustion, explicit unlimited
state, and `individual_limit` even when the backend returns `rate_limit=null`.
The latter contains monthly credit limit, credits used, remaining percentage,
and reset time. Successful refresh replaces the old rate-limit snapshot, so a
null window cannot leave stale percentages visible.

Token history has an independent fixed 30-minute cache window and a two-hour
stale threshold. The scheduler checks due state cheaply on every normal tick,
then starts at most one Codex App Server subprocess at a time for each due
healthy Session. Codex I/O happens outside the registry lock; only the final
small cache update is locked. This keeps Telegram responsive, avoids concurrent
subprocess bursts, and prevents a failed Session from blocking the others.

`account/usage/read` returns sparse daily token buckets and a lifetime summary.
OpenSwap stores only those aggregates. Rolling periods are calculated at render
time from dates, so no derived counters need periodic rewrites. Manual Refresh
forces collection; failed collection preserves the previous cache and marks it
stale.

Credentials are read once per unique assigned Session during a reconciliation
pass, not once per host. Adding the Codex workspace adds only one local file
read/write and any explicitly configured remote Codex targets. Targets are
rewritten only when the assigned OAuth fingerprint differs.

## Local publication

Local publication for both target kinds performs:

1. a bounded read of the current `auth.json`;
2. target-specific replacement of `openai` or the four managed Codex fields;
3. JSON serialization to a temporary file in the same directory;
4. file flush and `fsync`;
5. one final compare-and-swap digest check;
6. atomic `os.replace`;
7. directory `fsync` where the operating system supports it;
8. post-write account verification.

Applying `Make default` or `Use on this host` performs no OAuth, allowance, or
token-activity network request. The canonical slot is already the source of
truth: OpenSwap converts it to the selected target shape and publishes it
locally before scheduling any remote reconciliation. Credential and usage
refresh remain separate actions.

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
kind-aware merge, size, JSON, compare-and-swap, flush, atomic replacement, and
platform-specific permission rules as local storage.

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

The optional live Codex target is distinct from these isolated homes. Codex
documents file credentials under `CODEX_HOME/auth.json` (normally
`~/.codex/auth.json`); OpenSwap manages that published view only when
`codex.auth_file` is explicit. Keychain-backed credentials are outside this
file-based switching contract.

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
- exports require an allowlisted private chat and an explicit format choice;
- unmanaged provider entries and top-level fields are preserved;
- account identity mismatch never changes a slot;
- a changed target is never overwritten blindly;
- one offline host does not block other targets;
- desired offline assignments are retained;
- Telegram callbacks remain bounded and never perform SSH;
- configuration changes require restart rather than partial reload;
- canonical slots can rebuild every published target.
