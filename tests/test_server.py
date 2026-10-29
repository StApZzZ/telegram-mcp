"""Offline tests: Telethon's TelegramClient is replaced by an in-memory fake.

Every fake method first binds its arguments against the real TelegramClient method of the same name,
so a call production code makes with a wrong argument fails here as it would against Telegram. The
users, chats, dialogs and messages the fake returns are real Telethon objects.

Run: see "Development" in README.md.
"""

import asyncio
import functools
import importlib.util
import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telethon import TelegramClient, errors, functions, types, utils
from telethon._updates import EntityCache
from telethon.tl import custom

os.environ.update(
    TELEGRAM_MCP_CONFIG="/nonexistent/.env",
    TELEGRAM_API_ID="12345",
    TELEGRAM_API_HASH="test-hash",
    TELEGRAM_SESSION="/nonexistent/test.session",
)
_spec = importlib.util.spec_from_file_location("server", Path(__file__).resolve().parents[1] / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

NOW = datetime.now(UTC).replace(microsecond=0)
H = timedelta(hours=1)
NO_PHOTO = types.ChatPhotoEmpty()

ME = types.User(id=1, is_self=True, first_name="Me", username="me_user", access_hash=1, phone="79991234567")
BOB = types.User(id=2, first_name="Bob", username="bob", access_hash=2, contact=True, status=types.UserStatusOnline(NOW))
CAROL = types.User(id=3, first_name="Carol", access_hash=3, status=types.UserStatusRecently())
BOT = types.User(id=4, first_name="Alerts", bot=True, username="alerts_bot", access_hash=4)
DANA = types.User(id=5, first_name="Dana", access_hash=5)
INFRA = types.Channel(id=100, title="Infra", photo=NO_PHOTO, date=NOW, megagroup=True, access_hash=100)
INFRA_ALERTS = types.Channel(id=101, title="Infra Alerts", photo=NO_PHOTO, date=NOW, megagroup=True, access_hash=101)
NEWS = types.Channel(id=200, title="News", photo=NO_PHOTO, date=NOW, broadcast=True, username="news", access_hash=200)
FAMILY = types.Chat(id=300, title="Family", photo=NO_PHOTO, participants_count=4, date=NOW, version=1)
FLOOD = types.Channel(id=400, title="Flood", photo=NO_PHOTO, date=NOW, megagroup=True, access_hash=400)
ALL = (ME, BOB, CAROL, BOT, DANA, INFRA, INFRA_ALERTS, NEWS, FAMILY, FLOOD)
REPORT = types.Document(
    id=777, access_hash=1, file_reference=b"", date=NOW, mime_type="text/plain", size=11, dc_id=2,
    attributes=[types.DocumentAttributeFilename("report.txt")],
)  # fmt: skip


def run(coro):
    return asyncio.run(coro)


def mirrors(fake):
    """Bind each call against the TelegramClient method of the same name, then run the fake with the bound arguments.

    A keyword the real method would only swallow into its **kwargs is rejected too, since Telethon would ignore it.
    """
    real = getattr(TelegramClient, fake.__name__)
    sig = inspect.signature(real)
    self_name = next(iter(sig.parameters))
    var_kw = next((p.name for p in sig.parameters.values() if p.kind is p.VAR_KEYWORD), None)

    @functools.wraps(fake)
    def wrapper(self, *args, **kwargs):
        bound = sig.bind(self, *args, **kwargs)
        if var_kw and bound.arguments.get(var_kw):
            raise TypeError(f"{fake.__name__}() got unexpected keyword arguments {sorted(bound.arguments[var_kw])}")
        bound.apply_defaults()
        arguments = {k: v for k, v in bound.arguments.items() if k not in (self_name, var_kw)}
        return fake(self, **arguments)

    wrapper.__signature__ = sig
    wrapper.mirrored = (real, fake)
    return wrapper


async def _aiter(items):
    for item in items:
        yield item


class FakeTelegram:
    def __init__(self) -> None:
        self._self_id = ME.id  # read by Message._finish_init
        self._mb_entity_cache = EntityCache()
        self.calls: list[tuple[str, dict]] = []
        self.entities = {utils.get_peer_id(e): e for e in ALL}
        self.cached: set[int] = set()  # ids Telethon could resolve without an access hash lookup
        self.messages: dict[int, list] = {}
        self.downloads = {REPORT.id: b"hello world"}
        self.search_result = types.contacts.Found(my_results=[], results=[], chats=[], users=[])
        self.next_id = 1000

        self.add(BOB, 9, ME, "hi", 2 * H)
        self.add(BOB, 10, BOB, "can you look at the deploy?", H)
        self.add(CAROL, 20, CAROL, "old question", 120 * H)
        self.add(INFRA, 30, BOB, "@me_user ping", 3 * H, mentioned=True)
        self.add(INFRA, 31, CAROL, "unrelated chatter", 2 * H)
        self.add(INFRA, 32, BOB, None, 1.5 * H, action=types.MessageActionPinMessage())
        self.add(INFRA, 33, BOB, "more chatter", H, reactions=types.MessageReactions(
            results=[types.ReactionCount(types.ReactionEmoji("👍"), 2)]
        ))  # fmt: skip
        self.add(INFRA, 34, CAROL, "thread reply", 0.8 * H, reply_to=types.MessageReplyHeader(reply_to_msg_id=30))
        self.add(INFRA, 35, BOB, "the report", 0.5 * H, media=types.MessageMediaDocument(document=REPORT))
        self.add(NEWS, 40, NEWS, "release notes", 5 * H, post=True)
        self.add(FAMILY, 50, ME, "dinner?", 6 * H)
        self.add(FLOOD, 60, CAROL, "spam", H)
        self.add(ME, 70, ME, "note to self", 7 * H)
        self.add(BOT, 80, BOT, "Disk 91%", 0.5 * H)
        self.add(DANA, 90, DANA, "thanks", 3 * H)
        self.add(DANA, 91, ME, "you're welcome", 2 * H)
        self.dialog_rows = [
            dict(entity=BOB, unread=1, read_max=9),
            dict(entity=CAROL, unread=1, read_max=19),
            dict(entity=INFRA, unread=5, mentions=1, read_max=29),
            dict(entity=INFRA_ALERTS),
            dict(entity=NEWS, unread=1, read_max=39),
            dict(entity=FAMILY, read_max=50),
            dict(entity=FLOOD, unread=5, read_max=59, muted=True),
            dict(entity=ME, read_max=70),
            dict(entity=BOT, unread=1, read_max=79),
            dict(entity=DANA, read_max=91, marked=True),
        ]

    def add(self, chat, msg_id, sender, text, ago, action=None, **extra):
        private = isinstance(chat, types.User)
        fields = dict(
            id=msg_id,
            peer_id=utils.get_peer(chat),
            date=NOW - ago,
            out=sender is ME,
            # Incoming messages in private chats carry no from_id; channel posts carry none either.
            from_id=None if (private and sender is not ME) or extra.get("post") else utils.get_peer(sender),
        )
        if action is not None:
            msg = types.MessageService(action=action, **fields)
        else:
            msg = types.Message(message=text, **fields, **extra)
        msg._finish_init(self, self.entities, None)
        self.messages.setdefault(utils.get_peer_id(chat), []).append(msg)
        return msg

    def history(self, entity) -> list:
        return self.messages.get(utils.get_peer_id(entity), [])

    def dialog(self, entity, unread=0, mentions=0, read_max=0, muted=False, marked=False, archived=False):
        msgs = self.history(entity)
        raw = types.Dialog(
            peer=utils.get_peer(entity),
            top_message=msgs[-1].id if msgs else 0,
            read_inbox_max_id=read_max,
            read_outbox_max_id=0,
            unread_count=unread,
            unread_mentions_count=mentions,
            unread_reactions_count=0,
            unread_poll_votes_count=0,
            notify_settings=types.PeerNotifySettings(mute_until=NOW + 365 * 24 * H if muted else None),
            unread_mark=marked or None,
            folder_id=1 if archived else None,
        )
        return custom.Dialog(self, raw, self.entities, msgs[-1] if msgs else None)

    def sent(self, entity, text, reply_to):
        self.next_id += 1
        header = types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None
        return self.add(entity, self.next_id, ME, text, timedelta(0), reply_to=header)

    def called(self, name: str) -> list[dict]:
        return [args for method, args in self.calls if method == name]

    @mirrors
    def is_connected(self):
        return True

    @mirrors
    async def get_me(self, input_peer):
        return ME

    @mirrors
    def iter_dialogs(self, limit, **_):
        dialogs = sorted((self.dialog(**row) for row in self.dialog_rows), key=lambda d: d.date or server.EPOCH, reverse=True)
        self.cached.update(d.id for d in dialogs)
        return _aiter(dialogs[:limit])

    @mirrors
    async def get_entity(self, entity):
        self.calls.append(("get_entity", {"entity": entity}))
        if entity == "me":
            return ME
        if isinstance(entity, str):
            for e in self.entities.values():
                if (getattr(e, "username", None) or "").lower() == entity.lstrip("@").lower():
                    return e
            raise ValueError(f'No user has "{entity}" as username')
        if entity in self.cached:
            return self.entities[entity]
        raise ValueError(f"Could not find the input entity for {entity}")

    @mirrors
    async def get_messages(self, entity, limit, offset_id, min_id, max_id, search, filter, from_user, ids, reply_to, **_):
        self.calls.append(("get_messages", {"entity": entity, "limit": limit, "filter": filter, "reply_to": reply_to}))
        if ids is not None:
            return next((m for m in self.history(entity) if m.id == ids), None)
        msgs = self.history(entity) if entity is not None else [m for ms in self.messages.values() for m in ms]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=True)
        if min_id:
            msgs = [m for m in msgs if m.id > min_id]
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        if search:
            msgs = [m for m in msgs if search.lower() in (m.message or "").lower()]
        if from_user is not None:
            msgs = [m for m in msgs if m.sender_id == utils.get_peer_id(from_user)]
        if isinstance(filter, types.InputMessagesFilterMyMentions):
            msgs = [m for m in msgs if m.mentioned]
        if reply_to is not None:
            msgs = [m for m in msgs if m.reply_to and reply_to in (m.reply_to.reply_to_msg_id, m.reply_to.reply_to_top_id)]
        # Telethon's own default: one message unless both min_id and max_id are given.
        if limit is None and not (min_id and max_id):
            limit = 1
        return msgs[:limit] if limit is not None else msgs

    @mirrors
    async def send_message(self, entity, message, reply_to, silent, **_):
        self.calls.append(("send_message", {"entity": entity, "message": message, "reply_to": reply_to, "silent": silent}))
        return self.sent(entity, message, reply_to)

    @mirrors
    async def send_file(self, entity, file, caption, reply_to, silent, **_):
        self.calls.append(("send_file", {"entity": entity, "file": file, "caption": caption, "reply_to": reply_to}))
        files = file if isinstance(file, list) else [file]
        sent = [self.sent(entity, caption if i == 0 else "", reply_to) for i in range(len(files))]
        return sent if isinstance(file, list) else sent[0]

    @mirrors
    async def edit_message(self, entity, message, text, **_):
        self.calls.append(("edit_message", {"entity": entity, "message": message, "text": text}))
        msg = next(m for m in self.history(entity) if m.id == message)
        msg.message, msg.edit_date = text, NOW
        return msg

    @mirrors
    async def send_read_acknowledge(self, entity, message, max_id, clear_mentions, clear_reactions):
        self.calls.append(("send_read_acknowledge", {"entity": entity, "clear_mentions": clear_mentions, "clear_reactions": clear_reactions}))
        return True

    @mirrors
    def iter_download(self, file, **_):
        self.calls.append(("iter_download", {"file": file}))
        content = self.downloads[file.document.id]
        return _aiter([content[i : i + 4] for i in range(0, len(content), 4)])

    @mirrors
    async def __call__(self, request, ordered, flood_sleep_threshold):
        self.calls.append(("call", {"request": request}))
        if isinstance(request, functions.contacts.SearchRequest):
            return self.search_result
        return types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeTelegram()
    tg = server.Telegram()
    tg.client, tg.me = fake, ME
    monkeypatch.setattr(server, "tg", tg)
    return fake


def _mirrored() -> list:
    return [v.mirrored for v in vars(FakeTelegram).values() if hasattr(v, "mirrored")]


@pytest.mark.parametrize(("real", "impl"), _mirrored(), ids=lambda f: f.__name__)
def test_fake_is_sync_or_async_like_telethon(real, impl):
    assert inspect.iscoroutinefunction(real) == inspect.iscoroutinefunction(impl)


def test_fake_rejects_arguments_telethon_does_not_take(fake):
    with pytest.raises(TypeError):
        run(fake.get_messages(INFRA, 5, min_ids=3))
    with pytest.raises(TypeError, match="unexpected keyword"):
        run(fake.send_file(INFRA, "f.txt", captoin="typo"))


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/news/40", ("news", 40, None)),
        ("t.me/news", ("news", None, None)),
        ("https://t.me/s/news/40", ("news", 40, None)),
        ("https://t.me/c/100/30", (-1000000000100, 30, None)),
        ("https://t.me/c/1234567890/5", (-1001234567890, 5, None)),
        ("https://t.me/c/100/7/30", (-1000000000100, 30, 7)),
        ("https://telegram.me/bob?single", ("bob", None, None)),
        ("@bob", None),
        ("Infra", None),
    ],
)
def test_parse_link(link, expected):
    assert server._parse_link(link) == expected


@pytest.mark.parametrize("link", ["https://t.me/+AbCdEf", "https://t.me/joinchat/AbCdEf", "https://t.me/c/abc/1", "https://t.me/news/x"])
def test_invite_and_malformed_links_are_refused(link):
    with pytest.raises(ValueError):
        server._parse_link(link)


def test_resolve_chat_by_username_title_link_and_id(fake):
    fake.cached.add(utils.get_peer_id(INFRA))
    assert run(server._resolve_chat("@bob")) is BOB
    assert run(server._resolve_chat("Infra")) is INFRA  # exact title wins over "Infra Alerts"
    assert run(server._resolve_chat("family")) is FAMILY
    assert run(server._resolve_chat("https://t.me/news/40")) is NEWS
    assert run(server._resolve_chat("https://t.me/c/100/30")) is INFRA
    assert run(server._resolve_chat(-1000000000100)) is INFRA
    assert run(server._resolve_chat("me")) is ME


def test_ambiguous_or_unknown_titles_never_fall_back_to_public_usernames(fake):
    with pytest.raises(ValueError, match="ambiguous"):
        run(server._resolve_chat("infr"))
    with pytest.raises(ValueError, match="not found"):
        run(server._resolve_chat("news_typo"))
    assert not any(c["entity"] in ("news_typo", "infr") for c in fake.called("get_entity"))


def test_numeric_id_resolves_after_listing_chats(fake):
    assert run(server._resolve_chat("2")) is BOB
    assert [c["entity"] for c in fake.called("get_entity")] == [2, 2]


def test_get_unread_statuses(fake):
    result = run(server.get_unread())
    dms = {c["title"]: c for c in result["direct_messages"]}
    assert dms["Bob"]["status"] == "awaiting_reply"
    assert [m["id"] for m in dms["Bob"]["messages"]] == [10]
    assert dms["Carol"]["status"] == "awaiting_reply_stale"
    assert dms["Carol"]["collapsed"] is True and len(dms["Carol"]["messages"]) == 1
    assert dms["Dana"]["status"] == "you_replied_last" and dms["Dana"]["marked_unread"] is True
    [infra] = result["mentions"]
    assert infra["status"] == "awaiting_reply"
    assert [m["id"] for m in infra["mentioned_in"]] == [30]
    assert [m["id"] for m in infra["messages"]] == [30, 31, 33, 34, 35]  # the pin notice is skipped
    assert infra["messages"][2]["reactions"] == {"👍": 2}
    assert [c["title"] for c in result["bots"]] == ["Alerts"]
    assert result["groups"] == []  # Flood is muted
    assert result["channels"] == "omitted (include_channels=false)"
    assert result["summary"]["awaiting_reply"] == 2
    assert result["summary"]["awaiting_reply_stale"] == 1
    assert result["summary"]["skipped_muted"] == 1
    # The mention was fetched directly, not hoped for among the newest messages.
    assert any(isinstance(c["filter"], types.InputMessagesFilterMyMentions) for c in fake.called("get_messages"))


def test_get_unread_channels_and_muted_on_request(fake):
    result = run(server.get_unread(include_channels=True, include_muted=True))
    [news] = result["channels"]
    assert "status" not in news
    assert news["messages"][0]["author"] == "News (@news)" and news["messages"][0]["link"] == "https://t.me/news/40"
    assert [c["title"] for c in result["groups"]] == ["Flood"]


def test_read_chat_pages_back_oldest_first(fake):
    page = run(server.read_chat("Infra", limit=3))
    assert [m["id"] for m in page["messages"]] == [33, 34, 35]
    assert page["messages"][2]["media"] == {"type": "document", "name": "report.txt", "size_kb": 0.0, "downloadable": True}
    assert page["messages"][0]["link"] == "https://t.me/c/100/33"
    assert page["older_cursor"] == 33
    older = run(server.read_chat("Infra", limit=3, before=page["older_cursor"]))
    assert [m["id"] for m in older["messages"]] == [30, 31]
    assert older["service_messages_skipped"] == 1
    assert older["older_cursor"] == 30


def test_read_chat_since_and_thread(fake):
    recent = run(server.read_chat("Infra", since="3h"))
    assert [m["id"] for m in recent["messages"]] == [31, 33, 34, 35]
    assert "older_cursor" not in recent
    thread = run(server.read_chat("Infra", topic=30))
    assert [m["id"] for m in thread["messages"]] == [34]
    assert thread["messages"][0]["reply_to"] == 30
    assert fake.called("get_messages")[-1]["reply_to"] == 30


def test_send_message_reply_by_link_goes_to_that_chat(fake):
    result = run(server.send_message("ack", reply_to="https://t.me/c/100/30"))
    [call] = fake.called("send_message")
    assert call == {"entity": INFRA, "message": "ack", "reply_to": 30, "silent": False}
    assert result["status"] == "sent" and result["chat"] == "Infra" and result["author"] == "you"


def test_send_message_to_dm(fake):
    run(server.send_message("hi", chat="@bob", silent=True))
    [call] = fake.called("send_message")
    assert call["entity"] is BOB and call["reply_to"] is None and call["silent"] is True


def test_send_files_as_an_album_with_the_caption(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UPLOAD_DIRS", [tmp_path])
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name)
    result = run(server.send_message("files", chat="Infra", file_paths=[str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]))
    [call] = fake.called("send_file")
    assert call["file"] == [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")] and call["caption"] == "files"
    assert len(result["album_ids"]) == 2


def test_send_refuses_more_than_10_attachments_before_anything_is_sent(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UPLOAD_DIRS", [tmp_path])
    paths = []
    for i in range(11):
        (tmp_path / f"f{i}.txt").write_text("x")
        paths.append(str(tmp_path / f"f{i}.txt"))
    with pytest.raises(server.ToolError, match="At most 10 files"):
        run(server.send_message("files", chat="Infra", file_paths=paths))
    assert not fake.calls


@pytest.mark.parametrize("name", ["other.session", "id.pem", ".env", "sub/.hidden/file.txt"])
def test_send_refuses_session_key_and_hidden_files(fake, tmp_path, monkeypatch, name):
    monkeypatch.setattr(server, "UPLOAD_DIRS", [tmp_path])
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text("secret")
    with pytest.raises(server.ToolError, match="allowed directories"):
        run(server.send_message("x", chat="Infra", file_paths=[str(tmp_path / name)]))
    assert not fake.called("send_file")


def test_edit_message_only_own(fake):
    with pytest.raises(server.ToolError, match="own messages"):
        run(server.edit_message(10, "hacked", chat="@bob"))
    result = run(server.edit_message(9, "hello", chat="@bob"))
    assert result["previous_text"] == "hi" and result["text"] == "hello" and result["edited"] is True


def test_message_id_without_chat_is_refused(fake):
    with pytest.raises(server.ToolError, match="needs its chat"):
        run(server.edit_message(9, "hello"))


def test_react_and_unreact(fake):
    run(server.react("https://t.me/c/100/33", "👍"))
    run(server.react(33, "👍", chat="Infra", remove=True))
    added, removed = (c["request"] for c in fake.called("call"))
    assert isinstance(added, functions.messages.SendReactionRequest)
    assert (added.peer, added.msg_id, added.reaction) == (INFRA, 33, [types.ReactionEmoji("👍")])
    assert removed.reaction == []


def test_mark_read_clears_mentions_and_reactions(fake):
    assert run(server.mark_read("Infra")) == {"marked_read": "Infra"}
    assert fake.called("send_read_acknowledge") == [{"entity": INFRA, "clear_mentions": True, "clear_reactions": True}]


def test_search_messages_across_chats(fake):
    result = run(server.search_messages("chatter"))
    assert [(r["chat"], r["id"]) for r in result["results"]] == [("Infra", 33), ("Infra", 31)]
    with pytest.raises(server.ToolError, match="pass chat"):
        run(server.search_messages("x", from_user="@bob"))


def test_search_messages_from_one_person(fake):
    result = run(server.search_messages("", chat="Infra", from_user="@bob"))
    assert [r["id"] for r in result["results"]] == [35, 33, 30]


def test_find_users_lists_contacts_first_and_separates_chats(fake):
    fake.search_result = types.contacts.Found(
        my_results=[types.PeerUser(2)], results=[types.PeerUser(3), types.PeerChannel(200)], chats=[NEWS], users=[BOB, CAROL]
    )
    result = run(server.find_users("@b"))
    assert result["users"] == [
        {"id": 2, "title": "Bob", "type": "user", "username": "@bob", "contact": True, "status": "online"},
        {"id": 3, "title": "Carol", "type": "user", "contact": False, "status": "last seen recently"},
    ]
    assert result["chats"] == [{"id": -1000000000200, "title": "News", "type": "channel", "username": "@news"}]
    assert fake.called("call")[0]["request"].q == "b"


def test_find_users_caps_contacts_and_global_results_together(fake):
    # Telegram returns up to limit contacts plus up to limit global results.
    fake.search_result = types.contacts.Found(
        my_results=[types.PeerUser(2)], results=[types.PeerUser(3), types.PeerChannel(200)], chats=[NEWS], users=[BOB, CAROL]
    )
    result = run(server.find_users("b", limit=2))
    assert [u["id"] for u in result["users"]] == [2, 3]
    assert result["chats"] == []
    assert fake.called("call")[0]["request"].limit == 2


def test_get_me_masks_the_phone(fake):
    me = run(server.get_me())
    assert me["phone"] == "+79…67" and me["username"] == "@me_user"


def test_not_logged_in_creates_no_session_and_needs_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SESSION_PATH", tmp_path / "x.session")
    monkeypatch.setattr(server, "tg", server.Telegram())
    with pytest.raises(server.ToolError, match="login"):
        run(server.get_me())
    assert not (tmp_path / "x.session").exists()


def test_a_terminated_session_drops_the_client(fake, monkeypatch):
    async def revoked(*args, **kwargs):
        raise errors.AuthKeyUnregisteredError(request=None)

    monkeypatch.setattr(fake, "get_messages", revoked)
    with pytest.raises(server.ToolError, match="login"):
        run(server.read_chat("Infra"))
    assert server.tg.client is None


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    target = tmp_path / "downloads"
    target.mkdir()
    monkeypatch.setattr(server, "DOWNLOADS_DIR", target)
    return target


def test_download_never_overwrites_or_follows_symlinks(fake, downloads, tmp_path):
    (downloads / "report.txt").write_text("mine")
    # A dangling symlink at the next free name must not redirect the write outside the directory.
    (downloads / "report (1).txt").symlink_to(tmp_path / "escaped.txt")
    result = run(server.download_file("https://t.me/c/100/35"))
    assert Path(result["path"]) == downloads / "report (2).txt"
    assert Path(result["path"]).read_bytes() == b"hello world"
    assert (downloads / "report.txt").read_text() == "mine"
    assert not (tmp_path / "escaped.txt").exists()


def test_download_over_the_size_limit_is_refused(fake, downloads, monkeypatch):
    monkeypatch.setattr(server, "MAX_DOWNLOAD_BYTES", 5)
    with pytest.raises(server.ToolError, match="download limit"):
        run(server.download_file(35, chat="Infra"))
    # Refused on the reported size, before the content is requested.
    assert not fake.called("iter_download")
    assert not list(downloads.iterdir())


def test_download_limit_holds_when_the_reported_size_lies(fake, downloads, monkeypatch):
    monkeypatch.setattr(server, "MAX_DOWNLOAD_BYTES", 5)
    REPORT.size = 1
    try:
        with pytest.raises(server.ToolError, match="download limit"):
            run(server.download_file(35, chat="Infra"))
    finally:
        REPORT.size = 11
    assert not list(downloads.iterdir())


def test_download_without_a_file_is_refused(fake, downloads):
    with pytest.raises(server.ToolError, match="no photo or file"):
        run(server.download_file(33, chat="Infra"))
