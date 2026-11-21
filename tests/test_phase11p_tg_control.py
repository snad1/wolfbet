"""TG control socket: the agent protocol, per-user scoping, and the same paths as the Telegram commands.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11p_tg_control.py -q
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from tg import control_socket as ctl  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402

_GOLDEN = os.path.join(_HERE, "fixtures", "agent_proto_v1")
TOKEN = b"test-control-token-0123456789abcdefXYZ"
NOW_MS = 1_795_200_000_000


class FakeEngine:
    def __init__(self, session_id):
        self.session_id = session_id
        self.running = True
        self.paused = False
        self.stop_reason = None

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.running = False

    def _state_snapshot(self):
        return {"session_id": self.session_id, "paused": self.paused, "profit": 0.5}


@pytest.fixture
def engines(monkeypatch):
    """User 1 has one session in slot 1; user 2 has one in slot 2."""
    a, b = FakeEngine(11), FakeEngine(22)
    monkeypatch.setattr(tg_handlers, "active_engines", {1: {1: a}, 2: {2: b}})
    monkeypatch.setattr(tg_handlers, "recurring_state", {})
    return a, b


@pytest.fixture
def server():
    return ctl.ControlServer(TOKEN, "/unused", app=None, clock=lambda: NOW_MS / 1000)


_ids = iter(range(10**9))


def line(cmd, args=None, *, token=TOKEN, ts=NOW_MS, req_id=None, extra=None):
    fields = {"proto": 1, "id": req_id or f"r{next(_ids)}", "cmd": cmd, "args": args or {}, "ts": ts}
    fields["mac"] = ctl.sign(token, fields)
    fields.update(extra or {})
    return json.dumps(fields).encode() + b"\n"


def ask(server, raw):
    reply, _code = asyncio.run(server.process(raw))
    return json.loads(reply)


# ── wire format pinned to the dashboard's golden fixtures ──
def test_fixture_copies_match_the_contract_checksums():
    with open(os.path.join(_GOLDEN, "SHA256SUMS")) as f:
        for row in f.read().splitlines():
            digest, name = row.split()
            with open(os.path.join(_GOLDEN, name), "rb") as fixture:
                assert hashlib.sha256(fixture.read()).hexdigest() == digest, name


def test_signing_and_replies_match_the_agent_byte_for_byte():
    with open(os.path.join(_GOLDEN, "request.json")) as f:
        fixture = json.load(f)
    assert ctl.sign(fixture["token"].encode(), fixture["request"]) == fixture["request"]["mac"]
    with open(os.path.join(_GOLDEN, "responses.jsonl"), "rb") as f:
        for wire in f.read().splitlines(keepends=True):
            reply = json.loads(wire)
            code = reply["error"]["code"] if "error" in reply else None
            assert ctl.response_line(reply["id"], reply["ok"], reply.get("data"), code) == wire


def test_the_golden_request_is_accepted(engines):
    with open(os.path.join(_GOLDEN, "request.json")) as f:
        fixture = json.load(f)
    srv = ctl.ControlServer(fixture["token"].encode(), "/unused", app=None,
                            clock=lambda: fixture["request"]["ts"] / 1000)
    reply = ask(srv, json.dumps(fixture["request"]).encode())
    assert reply == {"id": "fixture-0001", "ok": False, "error": {"code": "not_found"}}


# ── per-user scoping ───────────────────────────────────────
def test_pause_and_resume_touch_only_the_named_users_session(server, engines):
    a, b = engines
    assert ask(server, line("pause", {"uid": 1}))["data"] == {"slots": [1]}
    assert a.paused and not b.paused
    assert ask(server, line("resume", {"uid": 1, "slot": 1}))["data"] == {"slots": [1]}
    assert not a.paused


def test_another_users_slot_is_not_found_and_keeps_betting(server, engines):
    a, b = engines
    reply = ask(server, line("stop", {"uid": 1, "slot": 2}))
    assert reply["error"] == {"code": "not_found"}
    assert b.running and a.running


def test_stop_uses_the_telegram_stop_path(server, engines):
    a, b = engines
    assert ask(server, line("stop", {"uid": 1}))["data"] == {"slots": [1]}
    assert not a.running and a.stop_reason == "Stopped by user"
    assert b.running


def test_a_user_without_sessions_gets_not_found(server, engines):
    assert ask(server, line("pause", {"uid": 3}))["error"] == {"code": "not_found"}


def test_several_sessions_need_a_slot(server, engines, monkeypatch):
    monkeypatch.setattr(tg_handlers, "active_engines", {1: {1: FakeEngine(1), 4: FakeEngine(4)}})
    assert ask(server, line("pause", {"uid": 1}))["error"] == {"code": "bad_request"}
    assert ask(server, line("pause", {"uid": 1, "slot": 4}))["data"] == {"slots": [4]}


def test_status_lists_only_the_users_sessions(server, engines):
    reply = ask(server, line("status", {"uid": 2}))
    assert reply["data"] == {"sessions": [{"session_id": 22, "paused": False, "profit": 0.5, "slot": 2}]}
    assert ask(server, line("status", {"uid": 3}))["data"] == {"sessions": []}


# ── set: the same rules as /set ────────────────────────────
@pytest.fixture
def saved(monkeypatch):
    store = {7: {"maxloss": None}}
    monkeypatch.setattr(tg_handlers, "load_user_config", lambda uid: dict(store.get(uid, {})))
    monkeypatch.setattr(tg_handlers, "save_user_config", lambda uid, cfg: store.__setitem__(uid, dict(cfg)))
    return store


def test_a_valid_setting_is_saved_like_slash_set(server, saved):
    reply = ask(server, line("set", {"uid": 7, "key": "maxloss", "value": "0.5"}))
    assert reply["data"] == {"message": "Set `maxloss` = `0.5`"}
    assert saved[7]["max_loss"] == 0.5


@pytest.mark.parametrize("key,value", [
    ("maxloss", "-1"), ("maxloss", "nan"), ("nosuchkey", "1"), ("rule", "sideways"),
    ("Max-Loss", "1"), ("maxloss", "x" * 257),
])
def test_a_refused_setting_saves_nothing(server, saved, key, value):
    reply = ask(server, line("set", {"uid": 7, "key": key, "value": value}))
    assert reply["error"] == {"code": "bad_request"}
    assert saved[7] == {"maxloss": None}


def test_logs_name_the_key_but_never_the_value(server, saved, caplog):
    with caplog.at_level(logging.INFO, logger="wolfbot"):
        ask(server, line("set", {"uid": 7, "key": "maxloss", "value": "0.123456789"}))
    assert "maxloss" in caplog.text
    assert "0.123456789" not in caplog.text


# ── start: the same path as /bet ───────────────────────────
def test_start_uses_start_session_and_the_users_chat(server, monkeypatch):
    calls = []

    async def fake_start(uid, chat_id, app, on_connecting=None):
        calls.append((uid, chat_id))
        return 3, types.SimpleNamespace(session_id=77), {}

    monkeypatch.setattr(tg_handlers, "start_session", fake_start)
    monkeypatch.setattr(tg_handlers, "_engine_chat_ids", {5: 500})
    assert ask(server, line("start", {"uid": 5}))["data"] == {"slot": 3, "session_id": 77}
    assert ask(server, line("start", {"uid": 6}))["ok"] is True
    assert calls == [(5, 500), (6, 6)]


def test_a_failed_start_is_bad_request(server, monkeypatch):
    async def fail(uid, chat_id, app, on_connecting=None):
        raise tg_handlers.StartError("Set your API key first: /setkey <token>")

    monkeypatch.setattr(tg_handlers, "start_session", fail)
    assert ask(server, line("start", {"uid": 5}))["error"] == {"code": "bad_request"}


# ── refused requests ───────────────────────────────────────
@pytest.mark.parametrize("raw,code", [
    (line("status", {"uid": 1}, token=b"another-token-0123456789abcdefXYZ"), "unauthorized"),
    (line("status", {"uid": 1}, ts=NOW_MS - 31_000), "unauthorized"),
    (line("status", {"uid": 1}, extra={"admin": True}), "bad_request"),
    (line("status", {"uid": True}), "bad_request"),
    (line("status", {"uid": 1, "slot": 1}), "bad_request"),
    (line("status", {}), "bad_request"),
    (line("shell", {"uid": 1}), "unknown_command"),
    (b"not json\n", "bad_request"),
    (b"[1, 2]\n", "bad_request"),
    (b"{" + b" " * ctl.MAX_REQUEST_BYTES + b"}\n", "too_large"),
])
def test_bad_requests_get_one_generic_code(server, engines, raw, code):
    assert ask(server, raw)["error"] == {"code": code}


def test_a_reused_id_is_refused(server, engines):
    raw = line("status", {"uid": 1}, req_id="once")
    assert ask(server, raw)["ok"] is True
    assert ask(server, raw)["error"] == {"code": "replayed"}


def test_a_bool_proto_is_refused(server):
    fields = {"proto": True, "id": "p", "cmd": "status", "args": {"uid": 1}, "ts": NOW_MS}
    fields["mac"] = ctl.sign(TOKEN, fields)
    assert ask(server, json.dumps(fields).encode())["error"] == {"code": "bad_request"}


# ── token file ─────────────────────────────────────────────
def test_token_is_created_owner_only_and_kept(tmp_path):
    path = str(tmp_path / "control.token")
    first = ctl.read_token(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert ctl.read_token(path) == first
    assert len(first) >= ctl.MIN_TOKEN_BYTES


def test_a_token_others_can_read_is_refused(tmp_path):
    path = tmp_path / "control.token"
    path.write_text(TOKEN.decode())
    os.chmod(path, 0o644)
    with pytest.raises(ctl.ControlError) as err:
        ctl.read_token(str(path))
    assert TOKEN.decode() not in str(err.value)


def test_a_weak_token_is_refused(tmp_path):
    path = tmp_path / "control.token"
    path.write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    os.chmod(path, 0o600)
    with pytest.raises(ctl.ControlError):
        ctl.read_token(str(path))


# ── over a real unix socket ────────────────────────────────
@pytest.fixture
def sock_dir():
    path = tempfile.mkdtemp(prefix="tgc", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _exchange(path, raw):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(raw)
    await writer.drain()
    reply = await reader.readline()
    tail = await reader.read()
    writer.close()
    return json.loads(reply), tail


def test_the_socket_serves_its_own_user_and_is_owner_only(sock_dir, engines):
    path = os.path.join(sock_dir, "control.sock")

    async def run():
        srv = ctl.ControlServer(TOKEN, path, app=None, clock=lambda: NOW_MS / 1000)
        await srv.start()
        try:
            mode = os.stat(path).st_mode & 0o777
            reply, _ = await _exchange(path, line("status", {"uid": 1}))
        finally:
            await srv.close()
        return mode, reply

    mode, reply = asyncio.run(run())
    assert mode == 0o600
    assert reply["ok"] is True and reply["data"]["sessions"][0]["slot"] == 1
    assert not os.path.exists(path)


def test_a_peer_outside_the_allowlist_is_refused_before_reading(sock_dir, engines, monkeypatch):
    path = os.path.join(sock_dir, "control.sock")
    monkeypatch.setattr(ctl.os, "getuid", lambda: -5)

    async def run():
        srv = ctl.ControlServer(TOKEN, path, app=None, clock=lambda: NOW_MS / 1000)
        await srv.start()
        try:
            return await _exchange(path, line("stop", {"uid": 1}))
        finally:
            await srv.close()

    reply, tail = asyncio.run(run())
    assert reply == {"id": None, "ok": False, "error": {"code": "unauthorized"}}
    assert tail == b""
    assert engines[0].running


def test_a_file_in_the_sockets_place_is_never_replaced(sock_dir):
    path = os.path.join(sock_dir, "control.sock")
    with open(path, "w") as f:
        f.write("keep me")
    srv = ctl.ControlServer(TOKEN, path, app=None)
    with pytest.raises(ctl.ControlError):
        asyncio.run(srv.start())
    with open(path) as f:
        assert f.read() == "keep me"
