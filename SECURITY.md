# Security

## Supported version

Security fixes are applied to the latest release and the `main` branch.

## Trust model

OpenSwap runs with the same operating-system identity that owns the managed
OpenCode credential file. Anyone who can read OpenSwap's configuration or state
can obtain live credentials and must be treated as a trusted local operator.

The trusted boundary includes:

- `config.toml`, which contains the Telegram bot token and allowlist;
- the configured storage directory and Session slots;
- the configured OpenCode `auth.json`;
- the official Codex executable;
- SSH keys and remote accounts used for optional multihost sync;
- the operating-system account running OpenSwap.

## Telegram authorization

The bot accepts an update only when:

- the chat type is `private`;
- the sender has a numeric user ID;
- that ID is present in `telegram.allowed_users`.

Unauthorized updates receive no response.

`/start` is the menu entrypoint. `/language` removes its own command message and
changes only a sparse per-user language preference. Every operational action is
an inline callback tied to the allowlisted private chat.

The bot cannot export tokens, execute arbitrary commands, or read arbitrary
paths. Imported documents are bounded to 1 MiB, read in memory, restricted to
accepted Codex/OpenCode shapes, and deleted from Telegram after processing.
OAuth callback messages are deleted immediately.

## Configuration

`config.toml` is the only configuration source and contains the bot token.
OpenSwap changes it to `0600` on POSIX systems. On Windows, place it in a
directory accessible only to the intended user; POSIX mode bits are not used as
a substitute for Windows ACLs.

The parser rejects unknown settings, invalid types, unsafe SSH destinations,
duplicate or oversized host names, relative remote paths, and ambiguous local
targets. Configuration is never logged and is not reloaded at runtime.

The repository ignores `config.toml` and `data/`. Never commit a real
configuration, state directory, uploaded credential document, or backup.

## Credential storage

Raw access and refresh tokens exist only in canonical Session slots and
published OpenCode-compatible `openai` entries. Registry fingerprints are
one-way SHA-256 values used to detect identity and external changes. Logs and
Telegram output never include tokens or ChatGPT account IDs.

Background usage failures are isolated per Session. The registry retains only a
bounded, whitespace-normalized diagnostic, failure count, timestamp, and refresh
source; a failed Session cannot suppress refresh or menu delivery for another
Session.

On POSIX systems, state directories use `0700`; credential and registry files
use `0600`. Writes use a temporary file in the destination directory, file
flush, `fsync`, final compare-and-swap verification, and atomic `os.replace`.
Directory metadata is flushed where supported.

Windows sharing violations receive a short fixed retry. The retry is bounded
and never becomes a background loop.

Other provider entries in `auth.json` are preserved. OpenSwap owns only the
OpenAI entry it manages.

## Session identity

New Sessions are accepted only after Codex returns a valid, refreshable ChatGPT
identity. Dead imports are rejected. Duplicate ChatGPT accounts are merged only
under the documented freshness policy.

Reauthorization is tied to the existing Session fingerprint. Signing into a
different ChatGPT account cannot silently replace an assigned Session. Failed
or cancelled reauthorization restores the previous credential document.

A Session cannot be removed while it is the default or assigned to a host.

## Codex boundary

OpenSwap delegates login, OAuth refresh, usage APIs, and earned reset operations
to the configured official Codex executable. It does not download, update, or
silently replace that executable.

Each Session uses an isolated Codex home. Refresh output is accepted only when
the ChatGPT account identity matches the existing slot.

## Remote synchronization

SSH destinations and remote Python commands are restricted to conservative
ASCII forms. OpenSwap invokes SSH without a shell-generated path or credential
argument; a bounded Python program is sent through stdin.

Remote publication enforces the same document size, JSON validation,
compare-and-swap, flush, atomic replacement, and platform-specific permission
rules as local publication. A target that changes concurrently is retried later
and never overwritten blindly.

Use dedicated SSH keys with the minimum necessary host and file permissions.
OpenSwap does not manage keys, agents, host trust, or privilege escalation.

## Earned resets

Reset credits are never consumed automatically. Telegram requires a separate
confirmation with a short expiry and one idempotency key. OpenSwap chooses the
nearest expiring eligible reset and refreshes usage after the result.

## Backups

Backups of `config.toml` or the storage directory are secrets. Preserve file
ownership and access controls, encrypt backups at rest, and never place them in
the source repository.

## Reports

Use GitHub's private vulnerability reporting from the repository Security tab.
Do not place tokens, `auth.json` documents, bot credentials, account IDs,
private paths, or SSH destinations in a public issue.

Treat unexpected credential disclosure, target replacement, Session identity
mismatch, or a Telegram allowlist bypass as a security issue. Stop OpenSwap,
revoke affected ChatGPT sessions and the bot token, rotate SSH credentials when
relevant, restore trusted state, and inspect logs before restarting.
