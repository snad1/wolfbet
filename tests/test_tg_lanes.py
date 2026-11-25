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
    button = opts["reply_markup"].inline_keyboard[-1][0]
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


def test_each_lane_gets_the_buttons_its_state_allows():
    assert tg_handlers._lane_buttons(LANES) == [
        [("Pause 1", "lane:pause:1"), ("Restart 1", "lane:restart:1"), ("Stop 1", "lane:stop:1")],
        [("Resume 2", "lane:resume:2"), ("Restart 2", "lane:restart:2"), ("Stop 2", "lane:stop:2")],
        [("Start 3", "lane:start:3")],
        [("Restart 4", "lane:restart:4"), ("Stop 4", "lane:stop:4")],
    ]
    for row in tg_handlers._lane_buttons(LANES):
        assert all(len(cb.encode()) <= 64 for _, cb in row)


@pytest.mark.parametrize("data", [
    "lane:pause:0", "lane:pause:100", "lane:pause:2;rm -rf /", "lane:delete:2", "lanego:stop:-1",
    "lanego:stop:2 --all", "lane:pause:02", "lanego::2", "lane:pause"])
def test_hostile_or_unknown_lane_callbacks_are_refused_and_nothing_runs(ctl, data):
    log = _tap(data)
    assert log == [("answer", "Unknown button.", {"show_alert": True})]
    assert ctl.calls == []


def _tap(data, user_id=OWNER_ID):
    upd, log = _query(user_id)
    upd.callback_query.data = data
    asyncio.run(tg_handlers.callback_handler(upd, types.SimpleNamespace(args=[])))
    return log


def test_another_chat_tapping_a_lane_button_is_refused(ctl):
    assert _tap("lanego:stop:2", OWNER_ID + 1) == [
        ("answer", "Only the owner can manage lanes.", {"show_alert": True})]
    assert ctl.calls == []


def _buttons(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_restart_asks_now_or_at_session_end_and_runs_nothing(ctl):
    log = _tap("lane:restart:2")
    kind, text, kw = log[1]
    assert (kind, text) == ("edit", "Restart lane 2?")
    assert _buttons(kw["reply_markup"]) == [[("Restart now", "lanego:restart:2"),
                                             ("At session end", "lanego:restart_end:2"),
                                             ("Cancel", "lanes_refresh")]]
    assert ctl.calls == []


def test_stopping_lane_1_warns_it_is_the_main_lane(ctl):
    assert _tap("lane:stop:1")[1][1] == "Stop lane 1? It is the main lane."
    assert _tap("lane:stop:2")[1][1] == "Stop lane 2?"


def _ran(ctl):
    return [cmd for cmd, _ in ctl.calls if cmd[1:] != ["lanes", "--json"]]


@pytest.mark.parametrize("data, argv", [
    ("lanego:pause:1", [CTL, "pause", "1"]),
    ("lanego:resume:2", [CTL, "resume", "2"]),
    ("lanego:stop:2", [CTL, "stop", "2"]),
    ("lanego:start:3", [CTL, "start", "3"]),
    ("lanego:restart:4", [CTL, "restart", "4"]),
    ("lanego:restart_end:1", [CTL, "restart", "1", "--at-boundary"]),
])
def test_a_confirmed_action_runs_one_verb_on_that_lane_only(ctl, monkeypatch, data, argv):
    real = tg_handlers.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["lanes", "--json"]:
            return real(cmd, **kw)
        ctl.calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=0, stdout="\x1b[32mLane done.\x1b[0m\n\n", stderr="")

    monkeypatch.setattr(tg_handlers.subprocess, "run", run)
    log = _tap(data)
    assert _ran(ctl) == [argv]
    assert [kw for cmd, kw in ctl.calls if cmd == argv][0]["timeout"] == 30
    kind, text, _ = log[1]
    assert kind == "edit" and text.startswith("Lane done.\n\nLane 1: running")


@pytest.mark.parametrize("data, reason", [
    ("lanego:stop:3", "Lane 3 is already stopped."),
    ("lanego:start:1", "Lane 1 is already running."),
    ("lanego:pause:2", "Lane 2 is already paused."),
    ("lanego:resume:1", "Lane 1 is not paused."),
    ("lanego:restart:9", "Lane 9 is not configured."),
])
def test_a_stale_button_reads_the_lane_again_and_runs_nothing(ctl, data, reason):
    text = _tap(data)[1][1]
    assert text.startswith(reason + "\n\n")
    assert _ran(ctl) == []


def test_a_failed_action_shows_what_wolfctl_said(ctl, monkeypatch):
    real = tg_handlers.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["lanes", "--json"]:
            return real(cmd, **kw)
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="Lane 3 did not start (failed).\nmore detail\n")

    monkeypatch.setattr(tg_handlers.subprocess, "run", run)
    assert _tap("lanego:start:3")[1][1].startswith("Lane 3 did not start (failed).\n\n")


def test_a_hung_wolfctl_gives_a_safe_message(ctl, monkeypatch):
    monkeypatch.setattr(tg_handlers, "APP_ENV", "production")
    real = tg_handlers.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["lanes", "--json"]:
            return real(cmd, **kw)
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(tg_handlers.subprocess, "run", run)
    assert _tap("lanego:stop:2")[1][1].startswith(
        "Lane 2: wolfctl did not answer. Check /lanes again.\n\n")


def test_cancel_brings_the_list_back(ctl):
    kind, text, kw = _tap("lanes_refresh")[1]
    assert kind == "edit" and text.startswith("Lane 1: running")
    assert _buttons(kw["reply_markup"])[-1] == [("Refresh", "lanes_refresh")]
