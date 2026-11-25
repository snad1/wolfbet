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
        [("Resume 2", "lane:resume:2"), ("Restart 2", "lane:restart:2"), ("Stop 2", "lane:stop:2"),
         ("Remove 2", "lane:remove:2")],
        [("Start 3", "lane:start:3"), ("Remove 3", "lane:remove:3")],
        [("Restart 4", "lane:restart:4"), ("Stop 4", "lane:stop:4"), ("Remove 4", "lane:remove:4")],
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
    ("lanego:remove:3", [CTL, "lane", "remove", "3"]),
    ("lanego:remove_end:2", [CTL, "lane", "remove", "2", "--after-session"]),
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


def test_remove_asks_now_or_at_session_end_and_runs_nothing(ctl):
    kind, text, kw = _tap("lane:remove:4")[1]
    assert (kind, text) == ("edit", "Remove lane 4?")
    assert _buttons(kw["reply_markup"]) == [[("Now", "lanego:remove:4"),
                                             ("At session end", "lanego:remove_end:4"),
                                             ("Cancel", "lanes_refresh")]]
    assert ctl.calls == []


@pytest.mark.parametrize("data, reason", [
    ("lanego:remove:1", "Lane 1 is the main lane and can't be removed."),
    ("lanego:remove_end:1", "Lane 1 is the main lane and can't be removed."),
    ("lanego:remove_end:3", "Lane 3 is stopped. Remove it with Now."),
    ("lanego:remove:9", "Lane 9 is not configured."),
])
def test_remove_refuses_lane_1_a_stopped_drain_and_a_missing_lane(ctl, data, reason):
    assert _tap(data)[1][1].startswith(reason + "\n\n")
    assert _ran(ctl) == []


def test_a_cli_error_printed_on_stdout_is_shown(ctl, monkeypatch):
    real = tg_handlers.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["lanes", "--json"]:
            return real(cmd, **kw)
        return types.SimpleNamespace(returncode=2, stdout="\x1b[31mNo lane 3.\x1b[0m\n", stderr="")

    monkeypatch.setattr(tg_handlers.subprocess, "run", run)
    assert _tap("lanego:remove:3")[1][1].startswith("No lane 3.\n\n")


@pytest.mark.parametrize("name, ok", [
    ("friend-B", True), ("night run", True), ("a" * 32, True), ("x_1", True),
    ("-x", False), ("a" * 33, False), ("bad;name", False), ("tab\tname", False), ("", False),
    (" lead", False), ("na\u00efve", False),
])
def test_lane_names_follow_the_cli_rule(name, ok):
    assert tg_handlers._lane_name_ok(name) is ok


def test_the_name_rule_is_the_clis_own_pattern():
    from shared.core.lanes import LANE_NAME_RE
    assert tg_handlers.LANE_NAME_RE is LANE_NAME_RE


def _command(handler, args, user_id=OWNER_ID):
    upd, sent = _update(user_id)
    asyncio.run(handler(upd, types.SimpleNamespace(args=args)))
    return sent


@pytest.mark.parametrize("args, question, data", [
    (["friend-B", "from", "2"], "Add the lane friend-B, copying lane 2's settings?",
     "laneadd:2:friend-B"),
    (["night", "run"], "Add the lane night run?", "laneadd:1:night run"),
    (["from", "x", "FROM", "4"], "Add the lane from x, copying lane 4's settings?",
     "laneadd:4:from x"),
])
def test_laneadd_asks_first_and_runs_nothing(ctl, args, question, data):
    (text, opts), = _command(tg_handlers.cmd_laneadd, args)
    assert text == question
    assert _buttons(opts["reply_markup"]) == [[("Add", data), ("Cancel", "lanes_refresh")]]
    assert len(data.encode()) <= 64
    assert ctl.calls == []


def test_lanerename_asks_first_and_runs_nothing(ctl):
    (text, opts), = _command(tg_handlers.cmd_lanerename, ["4", "night-run"])
    assert text == "Rename lane 4 to night-run?"
    assert _buttons(opts["reply_markup"]) == [[("Rename", "lanern:4:night-run"),
                                               ("Cancel", "lanes_refresh")]]
    assert ctl.calls == []


@pytest.mark.parametrize("handler, args, reply", [
    ("cmd_laneadd", [], "Usage: /laneadd NAME [from K]"),
    ("cmd_laneadd", ["-x"], tg_handlers.LANE_NAME_RULE),
    ("cmd_laneadd", ["a" * 33], tg_handlers.LANE_NAME_RULE),
    ("cmd_laneadd", ["x", "from", "0"], "Usage: /laneadd NAME [from K]"),
    ("cmd_lanerename", ["4"], "Usage: /lanerename K NAME"),
    ("cmd_lanerename", ["0", "x"], "Usage: /lanerename K NAME"),
    ("cmd_lanerename", ["4", "bad;name"], tg_handlers.LANE_NAME_RULE),
])
def test_bad_lane_commands_say_why_and_run_nothing(ctl, handler, args, reply):
    assert [t for t, _ in _command(getattr(tg_handlers, handler), args)] == [reply]
    assert ctl.calls == []


@pytest.mark.parametrize("handler, args", [
    ("cmd_laneadd", ["friend-B"]), ("cmd_lanerename", ["4", "x"])])
def test_lane_commands_from_another_chat_are_refused(ctl, handler, args):
    sent = _command(getattr(tg_handlers, handler), args, OWNER_ID + 1)
    assert [t for t, _ in sent] == ["Only the owner can manage lanes."]
    assert ctl.calls == []


def _run_named(ctl, monkeypatch, out, rc=0):
    real = tg_handlers.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["lanes", "--json"]:
            return real(cmd, **kw)
        ctl.calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(tg_handlers.subprocess, "run", run)


def test_a_confirmed_add_runs_lane_add_and_shows_what_wolfctl_said(ctl, monkeypatch):
    _run_named(ctl, monkeypatch, "Lane 5 (friend-B) added with lane 2's overrides.\n"
                                 "  lane 5 started\nOther lanes keep betting.\n")
    kind, text, _ = _tap("laneadd:2:friend-B")[1]
    assert _ran(ctl) == [[CTL, "lane", "add", "friend-B", "--from", "2"]]
    assert [kw for cmd, kw in ctl.calls if cmd[1] == "lane"][0]["timeout"] == 30
    assert kind == "edit" and text.startswith(
        "Lane 5 (friend-B) added with lane 2's overrides. Other lanes keep betting.\n\nLane 1:")


def test_an_add_over_the_limit_shows_the_cli_line(ctl, monkeypatch):
    _run_named(ctl, monkeypatch, "This bot already runs 3 lane(s), the limit.\n"
                                 "  Raise it with: wolfctl lanes --max N\n", rc=2)
    assert _tap("laneadd:1:x")[1][1].startswith("This bot already runs 3 lane(s), the limit.\n\n")


def test_a_confirmed_rename_runs_lane_rename_on_that_lane(ctl, monkeypatch):
    _run_named(ctl, monkeypatch, "Lane config updated\n  lane4.name: lane4 -> night-run\n")
    text = _tap("lanern:4:night-run")[1][1]
    assert _ran(ctl) == [[CTL, "lane", "rename", "4", "night-run"]]
    assert text.startswith("Lane 4 renamed to night-run.\n\n")


def test_renaming_a_missing_lane_runs_nothing(ctl):
    assert _tap("lanern:9:x")[1][1].startswith("Lane 9 is not configured.\n\n")
    assert _ran(ctl) == []


@pytest.mark.parametrize("data", [
    "laneadd:2:-x", "laneadd:0:x", "laneadd:2:a;b", "lanern:4:" + "a" * 33, "lanern:4:",
    "laneadd:2", "lanern:x:y", "laneadd:2:x\nrm"])
def test_forged_add_or_rename_buttons_are_refused(ctl, data):
    assert _tap(data) == [("answer", "Unknown button.", {"show_alert": True})]
    assert ctl.calls == []


def test_another_chat_tapping_add_is_refused(ctl):
    assert _tap("laneadd:1:x", OWNER_ID + 1) == [
        ("answer", "Only the owner can manage lanes.", {"show_alert": True})]
    assert ctl.calls == []
