# telegram-mcp

An [MCP](https://modelcontextprotocol.io) server for your personal [Telegram](https://telegram.org)
account. The assistant can see what's unread and whose turn it is to reply, read chats, forum
topics and comment threads, search, send messages and files, react and mark chats read.

It talks to Telegram as you, over MTProto via [Telethon](https://github.com/LonamiWebs/Telethon),
not as a bot. It sees exactly the chats you see.

It is a single file, `server.py`, run with [uv](https://docs.astral.sh/uv/). Its dependencies
are declared inline (PEP 723) and pinned in `server.py.lock`, so you don't need a virtualenv.
The server runs over stdio by default, or as an always-on Streamable HTTP service with `--http`.

## Tools

| Tool | What it does |
|---|---|
| `get_unread` | Everything unread, grouped into direct messages, mentions, groups, channels and bots. DMs and mentions have a status: `awaiting_reply`, `awaiting_reply_stale` or `you_replied_last`. Stale chats are collapsed to their last message; muted chats are skipped unless they mention you |
| `list_chats` | Your chats, filtered by type (`user`, `bot`, `group`, `channel`), unread or substring |
| `read_chat` | Recent messages, oldest first. Page back with `before` / `older_cursor`, filter with `since` (`2026-10-20`, `36h`, `7d`), or read one forum topic or comment thread with `topic` |
| `search_messages` | Full-text search in one chat or across all of them, optionally by sender |
| `find_users` | People, bots and public chats by name or @username, contacts first, with last-seen status |
| `download_file` | Save a message's photo or file into the downloads directory |
| `send_message` | Send to a chat or reply to a message, with Telegram markdown and up to 10 files as an album |
| `edit_message` | Edit one of your own messages |
| `react` | Add or remove an emoji reaction |
| `mark_read` | Mark a chat as read, mentions and reactions included |
| `get_me` | Connection check: the account the server is logged in as |

You can name a chat by `@username`, a `t.me` link, the numeric id from `list_chats`, its title
(exact, or a unique part of it) or `me` for Saved Messages. A bare title is looked up only among
your own chats, never as a public username, so a typo can't send a message to a stranger. You
can name a message by its `t.me` link, or by its id together with the chat.

Reading never marks anything as read. Deleting messages, joining or leaving chats and changing
account settings are deliberately not supported. Every tool carries MCP annotations
(`readOnlyHint`, `destructiveHint`, …). By default `send_message`, `edit_message` and `react` are
also marked `anthropic/requiresUserInteraction`, so Claude Code asks you before every call of them,
in every permission mode (see [Security](#security)). `TELEGRAM_CONFIRM_TOOLS` changes that list,
and `TELEGRAM_READ_ONLY=true` drops the writing tools altogether.

## Setup

Create a Telegram application at <https://my.telegram.org/apps> (any title, platform "Desktop")
and note its `api_id` and `api_hash`. Then run:

```bash
uv run --script server.py init
```

`init` asks a few questions, with the current value as the answer for Enter, and sets everything up:

- **Credentials**: your `api_id` and `api_hash`.
- **What the assistant may do**: read, plus send, edit and react only after you approve each call
  (the default); read, plus every change only after you approve it; or read only.
- **Files**: where downloads go and which folders attachments may come from.
- **How clients reach the server**: started by Claude Code when needed (stdio), or as a background
  HTTP service. For the service it generates the bearer token. On Linux with systemd it can also
  install the user service, start it and, with `loginctl enable-linger`, start it at boot
  (in WSL: whenever WSL starts).
- **Login**: a QR code or a phone number and code.
- **Registration in Claude Code**: `claude mcp add --scope user`. An existing `telegram` entry is
  replaced only if you agree.

It lists the changes to the config file and writes nothing until you confirm. It changes only the
keys you answered differently and keeps every other line. Run it again at any time to change a
setting.

### Manual setup

1. Put the credentials into the config file:

   ```bash
   mkdir -p ~/.config/telegram-mcp && chmod 700 ~/.config/telegram-mcp
   cp .env.example ~/.config/telegram-mcp/.env
   chmod 600 ~/.config/telegram-mcp/.env
   $EDITOR ~/.config/telegram-mcp/.env
   ```

2. Log in once. A QR code is printed; scan it in the Telegram app on your phone under
   **Settings → Devices → Link Desktop Device**. If you use two-step verification, you are asked
   for the password next.

   ```bash
   uv run --locked --script server.py login           # or: login --phone, for a phone number and code
   ```

   The session is stored in `~/.config/telegram-mcp/telegram.session` (mode 600). It shows up in
   **Settings → Devices** as *telegram-mcp*, where you can terminate it at any time.
   `uv run --script server.py logout` terminates it from this side and deletes the file.

| Variable | Required | Meaning |
|---|---|---|
| `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` | yes | Application credentials from my.telegram.org |
| `TELEGRAM_SESSION` | no | Session file (default `~/.config/telegram-mcp/telegram.session`) |
| `TELEGRAM_FLOOD_WAIT_MAX_SEC` | no | Telegram rate limits up to this long are waited out, longer ones fail the call (default `10`) |
| `TELEGRAM_DOWNLOADS_DIR` | no | Where files are saved (default `~/Downloads/telegram`). Saving outside it is refused |
| `TELEGRAM_MAX_DOWNLOAD_MB` | no | Largest file `download_file` will save (default `100`) |
| `TELEGRAM_UPLOAD_DIRS` | no | `:`-separated directories that attachments may come from (default: your home and the downloads directory) |
| `TELEGRAM_READ_ONLY` | no | `true` offers no tool that sends, edits, reacts, marks read or downloads (default `false`) |
| `TELEGRAM_CONFIRM_TOOLS` | no | Comma-separated writing tools that Claude Code asks you about on every call, or `none` (default `send_message,edit_message,react`). Others: `mark_read`, `download_file` |
| `TELEGRAM_MCP_TOKEN` | for `--http` | Bearer token clients must present, at least 16 characters |
| `TELEGRAM_MCP_HOST` / `TELEGRAM_MCP_PORT` | no | `--http` listen address, default `127.0.0.1:8733` |

Environment variables take precedence over the file. `TELEGRAM_MCP_CONFIG` points the server at
a different file.

## Running

Only one process should use a session file at a time. If several MCP clients need Telegram, run
the server once in HTTP mode and connect them all to it.

### stdio (the client starts the server)

```bash
claude mcp add --scope user telegram -- uv run --locked --script /path/to/telegram-mcp/server.py
```

### HTTP (the server runs on its own and clients connect to it)

```bash
# put the token into the config file as TELEGRAM_MCP_TOKEN=...
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
uv run --locked --script server.py --http
claude mcp add --scope user --transport http telegram http://127.0.0.1:8733/mcp \
  --header "Authorization: Bearer <token>"
```

To keep the server running in the background, use a systemd user service. `init` installs one
for you; to do it by hand, start from the template in
[`contrib/telegram-mcp.service`](contrib/telegram-mcp.service):

```bash
cp contrib/telegram-mcp.service ~/.config/systemd/user/   # adjust the path in ExecStart
systemctl --user daemon-reload
systemctl --user enable --now telegram-mcp
loginctl enable-linger "$USER"   # start at boot, not at first login
```

The service may start before you log in: tools report "not logged in" until the session exists,
then work without a restart.

## Security

Read [SECURITY.md](SECURITY.md). In short:

- The session file is a full login to your account. Keep it private, and terminate it in
  Settings → Devices if it leaks.
- Chat messages are untrusted input that can carry prompt injection. No server can filter that
  out reliably, so the server limits what an injected instruction can make the model do.
- In Claude Code, by default anything other people get to see takes your yes: `send_message`,
  `edit_message` and `react` prompt you on every call, even in auto and bypassPermissions modes,
  and no allow rule or hook skips the prompt. Other clients get the usual annotations, and the
  server instructions tell the model to confirm sends and edits with you first.
- If you only want the assistant to read, run in read-only mode (`init` answer 3, or
  `TELEGRAM_READ_ONLY=true`): the writing tools are not offered at all.
- The server only reads and writes files inside the configured directories, and never attaches
  hidden files, keys or session files.
- HTTP mode listens on loopback by default and requires a bearer token.

Telegram's [API terms](https://core.telegram.org/api/terms) apply to your use: read them first.
Flooding, spam and scraping are forbidden, Telegram may limit or ban accounts that misbehave, and
new accounts are watched more closely.

## Development

```bash
uv export --script server.py --format requirements.txt --no-header > /tmp/requirements.txt
uv run --no-project --with-requirements /tmp/requirements.txt --with pytest --with 'httpx>=0.27' pytest -q
```

`tests/test_server.py` runs the tools against a fake `TelegramClient`. Every fake method first
binds its arguments against the real Telethon method of the same name, so a call with a wrong
argument fails in the tests as it would against Telegram. The users, chats, dialogs and messages
it returns are real Telethon objects. `tests/test_init.py` drives `init` with scripted answers;
the commands it would run go to a fake that binds each call against the real `subprocess.run`.
`tests/test_http_dast.py` starts the real server in `--http`
mode and probes it over the network. It checks authentication, Host/Origin validation, body
limits, malformed JSON-RPC, file-path escapes and credential leaks. CI also runs ruff (with the
bandit rules), bandit, semgrep, CodeQL, pip-audit against the lockfile, and gitleaks over the full
history. After changing the dependencies in the script header, refresh the lockfile with
`uv lock --script server.py`.

## License

[MIT](LICENSE)
