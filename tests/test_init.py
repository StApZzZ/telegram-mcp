"""Tests of `server.py init`, the interactive setup.

Answers are scripted, and commands go to a fake that binds every call against the real subprocess.run
and, for the keywords run passes on, subprocess.Popen, so a call with a wrong argument fails here as
it would for real. Run: see "Development" in README.md.
"""

import asyncio
import importlib.util
import inspect
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.update(
    TELEGRAM_MCP_CONFIG="/nonexistent/.env",
    TELEGRAM_API_ID="12345",
    TELEGRAM_API_HASH="test-hash",
    TELEGRAM_SESSION="/nonexistent/test.session",
)
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("server_init", ROOT / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

HASH = "0123456789abcdef0123456789abcdef"
RUN = inspect.signature(subprocess.run)
POPEN = inspect.signature(subprocess.Popen)


class Answers:
    """Scripted terminal input: one answer per prompt, in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        assert self.answers, f"no scripted answer left for {prompt!r}"
        return self.answers.pop(0)


class FakeRun:
    """subprocess.run that records commands and answers them from `results` (command prefix -> rc, stdout)."""

    def __init__(self, results: dict[tuple, tuple[int, str]] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, *popenargs, **kwargs):
        bound = RUN.bind(*popenargs, **kwargs)
        POPEN.bind(*bound.arguments["popenargs"], **bound.arguments.get("kwargs", {}))
        args = list(popenargs[0])
        self.calls.append((args, kwargs))
        name = [Path(args[0]).name, *args[1:]]
        rc, out = next((r for prefix, r in self.results.items() if tuple(name[: len(prefix)]) == prefix), (0, ""))
        return subprocess.CompletedProcess(args, rc, out, "failed" if rc else "")

    def commands(self) -> list[list[str]]:
        return [[Path(args[0]).name, *args[1:]] for args, _ in self.calls]


def which(*available: str):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def setup(tmp_path, answers, secret=("",), run=None, available=(), **kwargs):
    return server._Setup(
        ask=answers,
        secret=Answers(*secret),
        run=run or FakeRun(),
        which=which(*available),
        config_path=tmp_path / "config" / ".env",
        session_path=tmp_path / "config" / "telegram.session",
        unit_dir=tmp_path / "units",
        **kwargs,
    )


def config(tmp_path) -> str:
    return (tmp_path / "config" / ".env").read_text()


def write_config(tmp_path, text: str) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / ".env").write_text(text)


def test_first_run_writes_only_what_differs_from_the_defaults_with_mode_600(tmp_path):
    run = FakeRun({("claude", "mcp", "get"): (1, "")})
    # api_id, profile, downloads, uploads, transport, write, login, register
    answers = Answers("12345", "", "", "", "", "y", "n", "y")
    setup(tmp_path, answers, secret=(HASH,), run=run, available=("uv", "claude")).main()

    assert config(tmp_path).splitlines()[1:] == ["TELEGRAM_API_ID=12345", f"TELEGRAM_API_HASH={HASH}"]
    assert stat.S_IMODE((tmp_path / "config" / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "config").stat().st_mode) == 0o700
    assert run.commands() == [
        ["claude", "mcp", "get", "telegram"],
        ["claude", "mcp", "add", "--scope", "user", "telegram",
         "-e", f"TELEGRAM_MCP_CONFIG={tmp_path / 'config' / '.env'}", "--",
         "/usr/bin/uv", "run", "--quiet", "--locked", "--script", str(ROOT / "server.py")],
    ]  # fmt: skip


def test_rerun_keeps_current_values_and_the_other_lines(tmp_path):
    write_config(tmp_path, f"# mine\nTELEGRAM_API_ID=777\nTELEGRAM_API_HASH={HASH}\nTELEGRAM_FLOOD_WAIT_MAX_SEC=5\nOTHER=x\n")
    # api_id, profile 3 (read-only: no file questions), transport, write, login, register
    answers = Answers("", "3", "", "y", "n", "n")
    setup(tmp_path, answers, secret=("",), available=("claude",)).main()

    assert config(tmp_path) == (
        f"# mine\nTELEGRAM_API_ID=777\nTELEGRAM_API_HASH={HASH}\nTELEGRAM_FLOOD_WAIT_MAX_SEC=5\nOTHER=x\n"
        "TELEGRAM_READ_ONLY=true\n"
    )
    assert "api_id [777]" in answers.prompts[0]


def test_declining_the_changes_writes_nothing_and_runs_nothing(tmp_path):
    run = FakeRun()
    setup(tmp_path, Answers("12345", "", "", "", "", "n"), secret=(HASH,), run=run, available=("uv", "claude")).main()
    assert not (tmp_path / "config" / ".env").exists()
    assert run.calls == []


def test_secrets_are_not_printed(tmp_path, capsys):
    setup(tmp_path, Answers("12345", "", "", "", "2", "", "y", "n"), secret=(HASH,)).main()
    out = capsys.readouterr().out
    token = server._read_config_file(tmp_path / "config" / ".env")["TELEGRAM_MCP_TOKEN"]
    assert len(token) >= 32
    assert HASH not in out and token not in out
    assert "TELEGRAM_API_HASH: (hidden)" in out


def test_invalid_answers_are_asked_again(tmp_path):
    answers = Answers("abc", "12345", "", "", "", "2", "80", "8800", "y", "n")
    setup(tmp_path, answers, secret=("short", HASH)).main()
    values = server._read_config_file(tmp_path / "config" / ".env")
    assert values["TELEGRAM_API_ID"] == "12345"
    assert values["TELEGRAM_API_HASH"] == HASH
    assert values["TELEGRAM_MCP_PORT"] == "8800"


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        # api_id, profile, [downloads, uploads,] transport, [write,] login
        (("", "1", "", "", "", "n"), {}),
        (("", "2", "", "", "", "y", "n"), {"TELEGRAM_CONFIRM_TOOLS": "send_message,edit_message,react,mark_read,download_file"}),
        (("", "3", "", "y", "n"), {"TELEGRAM_READ_ONLY": "true"}),
    ],
)
def test_permission_profiles(tmp_path, answers, expected):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\n")
    setup(tmp_path, Answers(*answers)).main()
    values = server._read_config_file(tmp_path / "config" / ".env")
    assert {k: v for k, v in values.items() if k not in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")} == expected


def test_a_custom_approval_list_can_be_kept(tmp_path):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\nTELEGRAM_CONFIRM_TOOLS=send_message\n")
    answers = Answers("", "", "", "", "", "n")
    setup(tmp_path, answers).main()
    assert answers.prompts[1] == "  Choice [4]: "
    assert "TELEGRAM_CONFIRM_TOOLS=send_message\n" in config(tmp_path)


def test_http_service_is_installed_restarted_and_registered_with_the_token(tmp_path):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\n")
    run = FakeRun({("loginctl", "show-user"): (0, "no\n")})
    # api_id, profile, downloads, uploads, transport 2, port, write, login, service, linger, register, replace
    answers = Answers("", "", "", "", "2", "", "y", "n", "y", "y", "y", "y")
    setup(tmp_path, answers, run=run, available=("uv", "systemctl", "loginctl", "claude")).main()

    token = server._read_config_file(tmp_path / "config" / ".env")["TELEGRAM_MCP_TOKEN"]
    unit = (tmp_path / "units" / "telegram-mcp.service").read_text()
    assert f"ExecStart=/usr/bin/uv run --quiet --locked --script {ROOT / 'server.py'} --http\n" in unit
    assert f"Environment=TELEGRAM_MCP_CONFIG={tmp_path / 'config' / '.env'}\n" in unit
    user = server.getpass.getuser()
    assert run.commands() == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "telegram-mcp"],
        ["systemctl", "--user", "restart", "telegram-mcp"],
        ["loginctl", "show-user", user, "--property=Linger", "--value"],
        ["loginctl", "enable-linger", user],
        ["claude", "mcp", "get", "telegram"],
        ["claude", "mcp", "remove", "telegram", "--scope", "user"],
        ["claude", "mcp", "add", "--scope", "user", "--transport", "http", "telegram",
         "http://127.0.0.1:8733/mcp", "--header", f"Authorization: Bearer {token}"],
    ]  # fmt: skip


def test_a_failing_systemctl_step_stops_the_service_setup(tmp_path, capsys):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\nTELEGRAM_MCP_TOKEN={'t' * 32}\n")
    run = FakeRun({("systemctl", "--user", "enable"): (1, "")})
    answers = Answers("", "", "", "", "2", "", "n", "y", "n")
    setup(tmp_path, answers, run=run, available=("uv", "systemctl", "loginctl", "claude")).main()
    assert ["systemctl", "--user", "restart", "telegram-mcp"] not in run.commands()
    assert "systemctl --user enable telegram-mcp failed" in capsys.readouterr().out


def test_switching_to_stdio_offers_to_stop_the_service(tmp_path):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\n")
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "telegram-mcp.service").write_text("[Service]\n")
    run = FakeRun()
    # api_id, profile, downloads, uploads, transport (default is 2 now), login, disable, register
    answers = Answers("", "", "", "", "1", "n", "y", "n")
    setup(tmp_path, answers, run=run, available=("uv", "systemctl", "claude")).main()
    assert answers.prompts[4] == "  Choice [2]: "
    assert ["systemctl", "--user", "disable", "--now", "telegram-mcp"] in run.commands()


def test_login_runs_against_the_new_file_not_values_taken_from_the_old_one(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "999")  # loaded from the old file
    monkeypatch.setenv("TELEGRAM_FLOOD_WAIT_MAX_SEC", "3")  # set by the user
    monkeypatch.setattr(server, "FROM_CONFIG_FILE", {"TELEGRAM_API_ID"})
    run = FakeRun()
    # api_id, profile, downloads, uploads, transport, write, login, QR
    setup(tmp_path, Answers("12345", "", "", "", "", "y", "y", "1"), secret=(HASH,), run=run).main()

    [(args, kwargs)] = run.calls
    assert args == [sys.executable, str(ROOT / "server.py"), "login"]
    assert "TELEGRAM_API_ID" not in kwargs["env"]
    assert kwargs["env"]["TELEGRAM_FLOOD_WAIT_MAX_SEC"] == "3"
    assert kwargs["env"]["TELEGRAM_MCP_CONFIG"] == str(tmp_path / "config" / ".env")


def test_an_existing_session_is_kept(tmp_path):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\n")
    (tmp_path / "config" / "telegram.session").write_text("")
    run = FakeRun()
    setup(tmp_path, Answers("", "", "", "", "", "n"), run=run, available=("claude",)).main()
    assert run.calls == []


def test_without_claude_code_the_command_is_printed_without_the_token(tmp_path, capsys):
    write_config(tmp_path, f"TELEGRAM_API_ID=1\nTELEGRAM_API_HASH={HASH}\nTELEGRAM_MCP_TOKEN={'s' * 32}\n")
    setup(tmp_path, Answers("", "", "", "", "2", "", "n")).main()
    out = capsys.readouterr().out
    assert "claude mcp add --scope user --transport http telegram http://127.0.0.1:8733/mcp --header" in out
    assert "s" * 32 not in out


def test_init_refuses_to_run_without_a_terminal(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "server.py"), "init"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, "TELEGRAM_MCP_CONFIG": str(tmp_path / ".env")},
        timeout=30,
    )
    assert proc.returncode == 1
    assert "run it in a terminal" in proc.stderr
    assert not (tmp_path / ".env").exists()


def test_the_unit_matches_the_contrib_template():
    def lines(text):
        return [line for line in text.splitlines() if line and not line.startswith(("#", "ExecStart="))]

    contrib = (ROOT / "contrib" / "telegram-mcp.service").read_text()
    assert lines(server._unit_text(["uv", "--http"], 8733, None)) == lines(contrib)
    assert "--http" in next(line for line in contrib.splitlines() if line.startswith("ExecStart="))


def test_systemd_words_are_quoted():
    assert server._systemd_arg("/usr/bin/uv") == "/usr/bin/uv"
    assert server._systemd_arg("/home/a b/server.py") == '"/home/a b/server.py"'
    assert server._systemd_arg("/tmp/100%") == "/tmp/100%%"


def test_config_values_are_validated(monkeypatch):
    assert server._confirm_tools(None) == server.DEFAULT_CONFIRM_TOOLS
    assert server._confirm_tools(" none ") == ()
    assert server._confirm_tools("send_message, mark_read,send_message") == ("send_message", "mark_read")
    with pytest.raises(SystemExit, match="delete_chat"):
        server._confirm_tools("delete_chat")
    monkeypatch.setenv("TELEGRAM_READ_ONLY", "maybe")
    with pytest.raises(SystemExit, match="TELEGRAM_READ_ONLY"):
        server._flag("TELEGRAM_READ_ONLY")


def test_write_tools_are_exactly_the_registered_tools_that_are_not_read_only():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools if not t.annotations.read_only_hint} == set(server.WRITE_TOOLS)
