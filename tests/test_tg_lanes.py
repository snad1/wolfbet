"""Telegram /lanes: owner only, read from `wolfctl lanes --json`, keys shown as label and last 4.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_tg_lanes.py -q
"""

import asyncio
import json
import os
import subprocess
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

from tg import handlers as tg_handlers  # noqa: E402
from tg.formatter import format_lanes  # noqa: E402

OWNER_ID = 4242
CTL = "/usr/local/bin/wolfctl"
SECRET = "sk-live-0123456789abcdefa1b2"

LANES = {"v": 1, "lanes": [
    {"lane": 3, "alive": False, "paused": False, "state": None},
    {"lane": 1, "alive": True, "paused": False, "state": {
        "lane_name": None, "key_ref": "default", "session_id": 70634, "profit": 1e-08,
        "currency": "usdt"}},
    {"lane": 2, "alive": True, "paused": True, "state": {
        "lane_name": "friend-A", "key_ref": "friend", "session_id": 14, "profit": -0.00012,
        "currency": "trx"}},
    {"lane": 4, "alive": True, "paused": False, "state": None},
], "totals": {"bps": 0.0, "profit": 0.0, "live": 3, "count": 4}}


def _label(key_ref):
    return {"friend": "friend (...a1b2)"}.get(key_ref, key_ref)


def test_format_lists_each_lane_in_order_with_state_key_session_and_profit():
    assert format_lanes(LANES, _label).splitlines() == [
        "Lane 1: running, key default, session 70634, +0.00000001 USDT",
        "Lane 2 · friend-A: paused, key friend (...a1b2), session 14, -0.00012000 TRX",
        "Lane 3: stopped",
        "Lane 4: no state yet",
    ]


def test_format_with_no_lanes():
    assert format_lanes({"lanes": []}, _label) == "No lanes."


def test_key_label_is_the_vault_label_and_last4_or_the_bare_id(monkeypatch):
    import shared.core.vault as vault

    def fake_info(service, key_id, log=None):
        assert service == "wolfbet"
        return types.SimpleNamespace(label="friend", last4="a1b2") if key_id == "friend" else None

    monkeypatch.setattr(vault, "vault_info", fake_info)
    assert tg_handlers._key_label("friend") == "friend (...a1b2)"
    assert tg_handlers._key_label("default") == "default"


def _update(user_id=OWNER_ID):
    sent = []

    async def reply_text(text, **kw):
        sent.append((text, kw))

    msg = types.SimpleNamespace(reply_text=reply_text)
    return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id),
                                 message=msg), sent


@pytest.fixture
def ctl(monkeypatch):
    calls = []
    reply = {"rc": 0, "out": json.dumps(LANES), "raise": None}

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if reply["raise"]:
            raise reply["raise"]
        return types.SimpleNamespace(returncode=reply["rc"], stdout=reply["out"], stderr="boom")

    monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
    monkeypatch.setattr(tg_handlers, "_ctl_path", lambda: CTL)
    monkeypatch.setattr(tg_handlers, "_key_label", _label)
    monkeypatch.setattr(tg_handlers.subprocess, "run", fake_run)
    return types.SimpleNamespace(calls=calls, reply=reply)


def _lanes(user_id=OWNER_ID):
    upd, sent = _update(user_id)
    asyncio.run(tg_handlers.cmd_lanes(upd, types.SimpleNamespace(args=[])))
    return sent


def test_other_chats_are_refused_and_nothing_runs(ctl):
    assert [t for t, _ in _lanes(OWNER_ID + 1)] == ["Only the owner can manage lanes."]
    assert ctl.calls == []


def test_owner_gets_the_list_from_wolfctl_with_a_refresh_button(ctl):
    sent = _lanes()
    (cmd, kw), = ctl.calls
    assert cmd == [CTL, "lanes", "--json"]
    assert kw["timeout"] == 10 and "shell" not in kw
    text, opts = sent[0]
    assert text.startswith("Lane 1: running, key default")
    assert "Lane 2 · friend-A: paused, key friend (...a1b2)" in text
    button = opts["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.callback_data) == ("Refresh", "lanes_refresh")
    assert "parse_mode" not in opts


@pytest.mark.parametrize("failure", ["rc", "json", "timeout", "missing"])
def test_a_failed_read_gives_a_safe_message(ctl, monkeypatch, failure):
    monkeypatch.setattr(tg_handlers, "APP_ENV", "production")
    if failure == "rc":
        ctl.reply["rc"] = 2
    elif failure == "json":
        ctl.reply["out"] = "not json " + SECRET
    elif failure == "timeout":
        ctl.reply["raise"] = subprocess.TimeoutExpired(CTL, 10)
    else:
        monkeypatch.setattr(tg_handlers, "_ctl_path", lambda: None)
    assert [t for t, _ in _lanes()] == ["Could not read the lanes. Try again."]


def test_outside_production_the_error_type_is_named(ctl, monkeypatch):
    monkeypatch.setattr(tg_handlers, "APP_ENV", "testing")
    ctl.reply["rc"] = 1
    assert _lanes()[0][0] == "Could not read the lanes. Try again. (RuntimeError)"


def _query(user_id=OWNER_ID):
    log = []

    async def answer(text=None, **kw):
        log.append(("answer", text, kw))

    async def edit_message_text(text, **kw):
        log.append(("edit", text, kw))

    q = types.SimpleNamespace(data="lanes_refresh", from_user=types.SimpleNamespace(id=user_id),
                              answer=answer, edit_message_text=edit_message_text)
    return types.SimpleNamespace(callback_query=q), log


def test_refresh_redraws_the_list_in_place(ctl):
    upd, log = _query()
    asyncio.run(tg_handlers.callback_handler(upd, types.SimpleNamespace(args=[])))
    assert log[0] == ("answer", None, {})
    kind, text, kw = log[1]
    assert kind == "edit" and text.startswith("Lane 1: running") and "reply_markup" in kw


def test_refresh_from_another_chat_is_refused(ctl):
    upd, log = _query(OWNER_ID + 1)
    asyncio.run(tg_handlers.callback_handler(upd, types.SimpleNamespace(args=[])))
    assert log == [("answer", "Only the owner can manage lanes.", {"show_alert": True})]
    assert ctl.calls == []
