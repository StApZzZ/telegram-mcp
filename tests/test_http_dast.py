"""Black-box (DAST) checks of the --http mode: a real server process on a free port, probed over the network.

No Telegram connection is made: there is no session, and every probe is rejected before the server
would talk to Telegram. Run: see "Development" in README.md.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

SERVER = Path(__file__).resolve().parents[1] / "server.py"
PREFIX = "TELEGRAM"
TOKEN = "dast-token-0123456789abcdef"
API_HASH = "not-a-real-api-hash"
ACCEPT = "application/json, text/event-stream"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(tmp_path: Path, **extra: str) -> dict:
    return {
        **os.environ,
        f"{PREFIX}_MCP_CONFIG": str(tmp_path / "missing.env"),
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": API_HASH,
        "TELEGRAM_SESSION": str(tmp_path / "missing.session"),
        f"{PREFIX}_DOWNLOADS_DIR": str(tmp_path / "downloads"),
        f"{PREFIX}_UPLOAD_DIRS": str(tmp_path / "uploads"),
        **extra,
    }


@contextlib.contextmanager
def _running_server(tmp_path: Path, **extra: str):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--http", "--port", str(port)],
        env=_env(tmp_path, **{f"{PREFIX}_MCP_TOKEN": TOKEN}, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(base, timeout=0.5)
                break
            except httpx.TransportError:
                if proc.poll() is not None:
                    pytest.fail(f"server exited: {proc.stderr.read().decode()}")
                time.sleep(0.1)
        yield f"{base}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("dast")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / ".secret").write_text("hidden")
    (tmp_path / "uploads" / "stolen.session").write_text("a session of another account")
    (tmp_path / "outside.txt").write_text("outside")
    with _running_server(tmp_path) as url:
        yield {"url": url, "tmp": tmp_path}


def _rpc(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


INIT = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "dast", "version": "0"}})


def _headers(**extra: str) -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Accept": ACCEPT, "Content-Type": "application/json", **extra}


def _payload(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


def _session(url: str) -> dict:
    resp = httpx.post(url, json=INIT, headers=_headers())
    assert resp.status_code == 200, resp.text
    headers = _headers(**{"mcp-session-id": resp.headers["mcp-session-id"]})
    httpx.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


def _call(url: str, headers: dict, tool: str, arguments: dict) -> dict:
    resp = httpx.post(url, json=_rpc("tools/call", {"name": tool, "arguments": arguments}, 7), headers=headers)
    return _payload(resp)["result"]


@pytest.mark.parametrize(
    "auth",
    [None, "Bearer wrong-token-wrong-token", f"Basic {TOKEN}", f"bearer {TOKEN}", f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}"],
)
def test_requests_without_the_exact_token_are_rejected(server, auth):
    headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if auth is not None:
        headers["Authorization"] = auth
    resp = httpx.post(server["url"], json=INIT, headers=headers)
    assert resp.status_code == 401
    assert "mcp-session-id" not in resp.headers


@pytest.mark.parametrize("path", ["/", "/mcp/", "/docs", "/.well-known/oauth-protected-resource", "/../etc/passwd"])
def test_every_path_requires_auth(server, path):
    assert httpx.get(server["url"].removesuffix("/mcp") + path).status_code == 401


def test_valid_token_initializes(server):
    resp = httpx.post(server["url"], json=INIT, headers=_headers())
    assert resp.status_code == 200
    assert _payload(resp)["result"]["serverInfo"]["name"]


def test_dns_rebinding_host_header_is_rejected(server):
    resp = httpx.post(server["url"], json=INIT, headers=_headers(Host="attacker.example"))
    assert resp.status_code in (400, 403, 421)


def test_foreign_origin_is_rejected(server):
    resp = httpx.post(server["url"], json=INIT, headers=_headers(Origin="http://attacker.example"))
    assert resp.status_code in (400, 403)


def test_cors_preflight_is_not_granted(server):
    resp = httpx.options(server["url"], headers={"Origin": "http://attacker.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in resp.headers


def test_oversized_body_is_rejected(server):
    resp = httpx.post(server["url"], content=b"x" * (5 * 1024 * 1024), headers=_headers())
    assert resp.status_code in (400, 413)


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"jsonrpc":"2.0"}', b"\xff\xfe"])
def test_malformed_json_rpc_does_not_crash(server, body):
    assert httpx.post(server["url"], content=body, headers=_headers()).status_code in (400, 422)
    assert httpx.post(server["url"], json=INIT, headers=_headers()).status_code == 200


def test_unknown_session_is_rejected(server):
    resp = httpx.post(server["url"], json=_rpc("tools/list"), headers=_headers(**{"mcp-session-id": "forged"}))
    assert resp.status_code in (400, 404)


def test_tools_are_annotated(server):
    headers = _session(server["url"])
    tools = _payload(httpx.post(server["url"], json=_rpc("tools/list", rpc_id=2), headers=headers))["result"]["tools"]
    assert tools and all("annotations" in t for t in tools)
    writers = [t["name"] for t in tools if not t["annotations"].get("readOnlyHint")]
    assert {"send_message", "edit_message"} <= set(writers)
    assert not any("delete" in t["name"] for t in tools)
    # By default everything other people see is approved by a human in Claude Code, whatever the permission mode.
    assert _flagged(tools) == {"send_message", "edit_message", "react"}


def _flagged(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools if (t.get("_meta") or {}).get("anthropic/requiresUserInteraction") is True}


def _tools(url: str) -> list[dict]:
    return _payload(httpx.post(url, json=_rpc("tools/list", rpc_id=2), headers=_session(url)))["result"]["tools"]


def test_confirm_tools_setting_chooses_the_flagged_tools(tmp_path):
    with _running_server(tmp_path, TELEGRAM_CONFIRM_TOOLS="send_message, mark_read") as url:
        assert _flagged(_tools(url)) == {"send_message", "mark_read"}
    with _running_server(tmp_path, TELEGRAM_CONFIRM_TOOLS="none") as url:
        assert _flagged(_tools(url)) == set()


def test_read_only_mode_offers_no_writing_tool(tmp_path):
    with _running_server(tmp_path, TELEGRAM_READ_ONLY="true") as url:
        tools = _tools(url)
        instructions = _payload(httpx.post(url, json=INIT, headers=_headers()))["result"]["instructions"]
    assert tools and all(t["annotations"]["readOnlyHint"] for t in tools)
    assert "read-only" in instructions


@pytest.mark.parametrize("extra", [{"TELEGRAM_CONFIRM_TOOLS": "delete_chat"}, {"TELEGRAM_READ_ONLY": "maybe"}])
def test_an_invalid_permission_setting_stops_the_server(tmp_path, extra):
    proc = subprocess.run(
        [sys.executable, str(SERVER), "--http", "--port", str(_free_port())],
        env=_env(tmp_path, **{f"{PREFIX}_MCP_TOKEN": TOKEN}, **extra),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert next(iter(extra)) in proc.stderr


@pytest.mark.parametrize("save_dir", ["../", "/tmp", "../../.ssh", ".hidden"])
def test_download_cannot_escape_downloads_dir(server, save_dir):
    result = _call(server["url"], _session(server["url"]), "download_file", {"message": "https://t.me/c/1/1", "save_dir": save_dir})
    assert result["isError"] and "allowed directories" in result["content"][0]["text"]


@pytest.mark.parametrize("name", ["uploads/.secret", "uploads/stolen.session", "outside.txt", "missing.env"])
def test_send_cannot_read_hidden_session_or_outside_files(server, name):
    result = _call(
        server["url"],
        _session(server["url"]),
        "send_message",
        {"message": "x", "chat": "@nobody", "file_paths": [str(server["tmp"] / name)]},
    )
    assert result["isError"]
    assert "allowed directories" in result["content"][0]["text"]


def test_without_a_session_tools_fail_cleanly_and_leak_no_credentials(server):
    result = _call(server["url"], _session(server["url"]), "get_me", {})
    assert result["isError"] and "login" in result["content"][0]["text"]
    assert API_HASH not in json.dumps(result)
    # The failed call must not have left an empty session file behind.
    assert not (server["tmp"] / "missing.session").exists()


@pytest.mark.parametrize("token", ["", "short"])
def test_http_mode_refuses_to_start_without_a_strong_token(tmp_path, token):
    proc = subprocess.run(
        [sys.executable, str(SERVER), "--http", "--port", str(_free_port())],
        env=_env(tmp_path, **{f"{PREFIX}_MCP_TOKEN": token}),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode != 0 and b"MCP_TOKEN" in proc.stderr
