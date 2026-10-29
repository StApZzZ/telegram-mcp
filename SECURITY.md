# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability**
on this repository. Do not open a public issue. I aim to respond within a week. Only the
latest commit on `main` is supported.

## Threat model

This server holds a logged-in Telegram session with full access to one personal account, and acts
on behalf of an AI model. The main risks, and what the server does about each, are below.

### The session file

Whoever has the session file is logged in as you. They can read every chat and send messages
in your name, from anywhere, until the session is terminated.

- `server.py login` creates the file under a `0077` umask: the file is `0600` and its directory
  `0700`. The systemd unit template sets `UMask=0077` too.
- The session is registered with the device name *telegram-mcp*. It is listed in Telegram's
  **Settings → Devices**, where you can terminate it on its own without touching your other
  devices. `server.py logout` terminates it from the server's side and deletes the file.
- The server never creates a session by itself. Without the file, tools fail with "not logged in"
  and nothing is written to disk.
- No tool can attach a `*.session` file to a message, wherever it lies. See "Local file access".

If the file leaks, terminate the session in Settings → Devices first, then log in again.

### Prompt injection through message content

Anyone who can write in a chat you are in, or message you, controls text that reaches the model.
A malicious message may try to make the model send messages, leak files or edit your messages.

- By default `send_message`, `edit_message` and `react`, the tools whose effect other people see,
  carry `_meta["anthropic/requiresUserInteraction"]: true`. `TELEGRAM_CONFIRM_TOOLS` (or the
  `init` question "What may the assistant do?") sets which writing tools carry it; `none` turns
  the prompts off, at your own risk. Claude Code then shows its permission prompt
  on every call, in every permission mode including `auto` and `bypassPermissions`. It offers no
  "don't ask again", lets no allow rule, hook or auto-mode classifier approve the call instead, and
  denies it when no person can answer (`dontAsk`, non-interactive runs). The prompt shows the
  exact chat, text and file paths.
- With `TELEGRAM_READ_ONLY=true` the writing tools are not registered at all, so no client is
  offered them and no injected instruction can call them.
- Every tool is annotated (`readOnlyHint` / `destructiveHint`), so other clients can require
  approval for anything that writes.
- The server instructions tell the model to treat message content as untrusted data, and to
  show you the exact target and text for confirmation before `send_message` or `edit_message`.
- Deleting messages, joining or leaving chats, managing contacts and changing account settings
  are not implemented. `edit_message` refuses messages by other people, and reading never changes
  unread state.
- A bare chat name is resolved only among your own chats. It is never resolved as a public
  username, so neither a typo nor an injected name can route a message to a stranger. Reaching
  someone outside your chats takes an explicit `@username` or `t.me` link. Invite links are
  refused.

What this does not cover:

- The prompt protects you only if you read it. Check the chat and the text before you approve.
- An injected message can still mislead the model in what it tells you, for example in a summary
  of unread chats. Open the source message before you act on anything important.
- Other tools in the same session, such as a shell, web access or another messaging server, are
  outside this server's control. Your client's permission settings decide what they may do.
- `mark_read` and `download_file` are not flagged, so that prompts stay for what matters.
  `mark_read` reveals no content, only read receipts to the senders; downloads stay local and
  confined (see below). Both follow your client's normal approval rules.
- Clients other than Claude Code ignore the flag. Keep tool-call approval on there for
  `send_message`, `edit_message` and `react`.

### Local file access

`send_message` reads local files to attach, and `download_file` writes files. Both are confined:

- **Reads** must resolve, after following symlinks, into a directory listed in
  `TELEGRAM_UPLOAD_DIRS`. The default is your home directory and the downloads directory.
- **Writes** go only into `TELEGRAM_DOWNLOADS_DIR` or its subdirectories. Files are created
  with `O_EXCL`, so an existing file is never overwritten and a symlink at the target name, even
  a dangling one, is never followed.
- **Size**: downloads are streamed to disk and capped at `TELEGRAM_MAX_DOWNLOAD_MB` (default
  100) on the bytes actually received; an oversized file is refused and the partial file removed.
  A message takes at most 10 attachments.
- Hidden paths (any component starting with `.`, such as `~/.ssh` or `~/.config`), the server's
  own config file and key material (`*.session`, `*.session-journal`, `*.pem`, `*.key`, `*.pfx`,
  `*.p12`, `*.kdbx`) are always refused.
- File names from Telegram are sanitized: no path separators, no leading dots.

If your upload directories hold sensitive files that are not hidden, narrow
`TELEGRAM_UPLOAD_DIRS`.

### HTTP mode (`--http`)

- The server listens on `127.0.0.1` by default. On any other address it prints a warning: the
  transport is plain HTTP, so put a TLS-terminating proxy in front of it.
- A bearer token (`TELEGRAM_MCP_TOKEN`, at least 16 characters) is mandatory. The server refuses
  to start without one. Tokens are compared in constant time, and every path requires one.
- On loopback, Host and Origin headers are validated, which blocks DNS rebinding. CORS is not
  enabled, so browser pages cannot call the server.
- Request bodies are limited to 4 MiB, and access logging is off.

### Credentials

- `server.py init` writes the config file atomically with mode `0600`, and creates a missing
  config directory with mode `0700`. It reads `api_hash` without echoing it, and never prints
  `api_hash` or the token. Commands it runs (`systemctl`, `loginctl`, `claude`, the login) get
  argument lists, never a shell.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` and the MCP token are read from environment variables
  or a local `.env` file (keep it `chmod 600`). They are never logged or returned in tool results.
- `get_me` masks your phone number.
- The client does not subscribe to live updates: it only makes the requests a tool call needs.

## Security testing

CI runs on every push and pull request, and weekly to catch new CVEs:

| Kind | Tools |
|---|---|
| SAST | ruff (flake8-bandit rules), bandit, semgrep (`p/python`, `p/security-audit`, `p/secrets`, `p/owasp-top-ten`), CodeQL `security-extended` |
| DAST | `tests/test_http_dast.py`: a real server process probed for auth bypass, DNS rebinding, CORS, oversized and malformed requests, forged sessions, path escapes, session-file theft and credential leaks |
| SCA | pip-audit (OSV) against the hashed lockfile `server.py.lock` |
| Secrets | gitleaks over the full git history, plus a pre-commit hook |

GitHub Actions are pinned to commit SHAs, and Dependabot keeps them current.
