"""Telegram /sessions: owner only, read from `wolfctl stats --json`, 10 per page.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_tg_sessions.py -q
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

import tg.config as tg_config  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402
from tg.formatter import format_cli_session_detail, format_cli_session_line  # noqa: E402

OWNER_ID = 4242
CTL = "/usr/local/bin/wolfctl"


def _session(sid, lane=1, started="2026-11-25T12:04:00", ended="2026-11-25T12:31:10", **kw):
    row = {"id": sid, "started_at": started, "ended_at": ended, "currency": "USDT",
           "strategy": "Flat", "multiplier": 2.0, "base_bet": 1e-08, "total_bets": 9812,
           "wins": 4900, "losses": 4912, "profit": 0.00012, "wagered": 0.5,
           "start_balance": 1.0, "end_balance": 1.00012, "lane": lane}
    row.update(kw)
    return row


def test_a_closed_session_line():
    assert format_cli_session_line(_session(14), show_lane=False) == (
        "#14 · 11-25 12:04-12:31 · 9,812 bets · +0.00012000 USDT")


def test_a_running_session_and_one_across_midnight():
    assert "· 11-25 12:04-running ·" in format_cli_session_line(_session(3, ended=None), False)
    line = format_cli_session_line(_session(4, started="2026-11-24T23:50:00",
                                            ended="2026-11-25T00:20:00"), False)
    assert "· 11-24 23:50-11-25 00:20 ·" in line


def test_the_lane_tag_shows_only_for_lanes_above_1_in_the_all_lanes_list():
    assert format_cli_session_line(_session(5, lane=2), True).startswith("L2 · #5 ·")
    assert format_cli_session_line(_session(6, lane=1), True).startswith("#6 ·")
    assert format_cli_session_line(_session(5, lane=2), False).startswith("#5 ·")


def test_the_detail_lists_times_bets_money_and_strategy():
    text = format_cli_session_detail(_session(14, lane=2))
    assert text.splitlines() == [
        "Session #14 · lane 2",
        "Start: 2026-11-25 12:04:00",
        "End: 2026-11-25 12:31:10",
        "Duration: 0h 27m 10s",
        "Bets: 9,812 (W 4,900 / L 4,912, win rate 49.9%)",
        "Wagered: 0.50000000 USDT",
        "Profit: +0.00012000 USDT",
        "Balance: 1.00000000 -> 1.00012000",
        "Strategy: Flat, 2x, base 0.00000001",
    ]
    assert "End: running" in format_cli_session_detail(_session(3, ended=None))


@pytest.fixture
def ctl(monkeypatch):
    calls = []
    reply = {"rows": [_session(i, lane=2) for i in range(30, 0, -1)], "rc": 0,
             "out": None, "raise": None}

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if reply["raise"]:
            raise reply["raise"]
        rows = reply["rows"]
        if "--before" in cmd:
            before = int(cmd[cmd.index("--before") + 1])
            rows = [r for r in rows if r["id"] < before]
        out = reply["out"] if reply["out"] is not None else json.dumps(
            {"v": 1, "sessions": rows[:20], "totals": {}})
        return types.SimpleNamespace(returncode=reply["rc"], stdout=out, stderr="")

    monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
    monkeypatch.setattr(tg_handlers, "_ctl_path", lambda: CTL)
    monkeypatch.setattr(tg_handlers.subprocess, "run", fake_run)
    monkeypatch.setattr(tg_config, "DASHBOARD_URL", "https://dash.test")
    return types.SimpleNamespace(calls=calls, reply=reply)


def _command(args, user_id=OWNER_ID):
    sent = []

    async def reply_text(text, **kw):
        sent.append((text, kw))

    upd = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id),
                                message=types.SimpleNamespace(reply_text=reply_text))
    asyncio.run(tg_handlers.cmd_sessions(upd, types.SimpleNamespace(args=args)))
    return sent


def _tap(data, user_id=OWNER_ID):
    log = []

    async def answer(text=None, **kw):
        log.append(("answer", text, kw))

    async def edit_message_text(text, **kw):
        log.append(("edit", text, kw))

    q = types.SimpleNamespace(data=data, from_user=types.SimpleNamespace(id=user_id),
                              answer=answer, edit_message_text=edit_message_text)
    asyncio.run(tg_handlers.callback_handler(types.SimpleNamespace(callback_query=q),
                                             types.SimpleNamespace(args=[])))
    return log


def _buttons(markup):
    return [[(b.text, b.callback_data or b.url) for b in row] for row in markup.inline_keyboard]


def test_sessions_for_one_lane_lists_10_with_buttons_and_older(ctl):
    (text, opts), = _command(["2"])
    (cmd, kw), = ctl.calls
    assert cmd == [CTL, "stats", "--json", "--lane", "2"]
    assert kw["timeout"] == 10 and "shell" not in kw
    lines = text.splitlines()
    assert len(lines) == 10 and lines[0].startswith("#30 · ") and lines[-1].startswith("#21 · ")
    assert "parse_mode" not in opts
    rows = _buttons(opts["reply_markup"])
    assert rows[0] == [(f"#{i}", f"sessd:2:{i}") for i in range(30, 25, -1)]
    assert rows[-1] == [("Older", "sess:2:21")]


def test_older_shows_the_next_page_in_place(ctl):
    kind, text, kw = _tap("sess:2:21")[1]
    assert ctl.calls[0][0] == [CTL, "stats", "--json", "--lane", "2", "--before", "21"]
    assert kind == "edit" and text.splitlines()[0].startswith("#20 · ")
    assert _buttons(kw["reply_markup"])[-1] == [("Older", "sess:2:11")]


def test_the_last_page_has_no_older_button(ctl):
    ctl.reply["rows"] = [_session(i, lane=2) for i in range(5, 0, -1)]
    (text, opts), = _command(["2"])
    assert len(text.splitlines()) == 5
    assert all(b[0] != "Older" for row in _buttons(opts["reply_markup"]) for b in row)


def test_all_lanes_reads_without_a_lane_and_tags_lines(ctl):
    ctl.reply["rows"] = [_session(9, lane=2), _session(8, lane=1)]
    (text, _), = _command([])
    assert ctl.calls[0][0] == [CTL, "stats", "--json"]
    assert text.splitlines()[0].startswith("L2 · #9 ·") and text.splitlines()[1].startswith("#8 ·")


@pytest.mark.parametrize("args, reply", [
    (["2"], "No sessions on lane 2 yet."), ([], "No sessions yet.")])
def test_no_sessions_yet(ctl, args, reply):
    ctl.reply["rows"] = []
    assert [t for t, _ in _command(args)] == [reply]


def test_no_database_yet_reads_as_no_sessions(ctl):
    ctl.reply.update(rc=1, out=json.dumps({"v": 1, "error": {"code": "no_database",
                                                            "message": "No database found."}}))
    assert [t for t, _ in _command(["2"])] == ["No sessions on lane 2 yet."]


@pytest.mark.parametrize("failure", ["rc", "json", "timeout"])
def test_a_failed_read_gives_a_safe_message(ctl, monkeypatch, failure):
    monkeypatch.setattr(tg_handlers, "APP_ENV", "production")
    if failure == "rc":
        ctl.reply.update(rc=2, out="Traceback (most recent call last)")
    elif failure == "json":
        ctl.reply["out"] = "not json"
    else:
        ctl.reply["raise"] = subprocess.TimeoutExpired(CTL, 10)
    assert [t for t, _ in _command(["2"])] == ["Could not read the sessions. Try again."]


@pytest.mark.parametrize("args", [["x"], ["0"], ["100"], ["2", "3"], ["-1"]])
def test_bad_arguments_answer_the_usage_line(ctl, args):
    assert [t for t, _ in _command(args)] == ["Usage: /sessions [lane]"]
    assert ctl.calls == []


def test_another_chat_is_refused_and_nothing_runs(ctl):
    assert [t for t, _ in _command(["2"], OWNER_ID + 1)] == ["Only the owner can manage lanes."]
    assert _tap("sessd:2:14", OWNER_ID + 1) == [
        ("answer", "Only the owner can manage lanes.", {"show_alert": True})]
    assert ctl.calls == []


@pytest.mark.parametrize("data", [
    "sess:2", "sessd:2:x", "sessd:100:1", "sess:2:1;rm", "sessd:2:12345678901", "sessd::1"])
def test_forged_session_buttons_are_refused(ctl, data):
    assert _tap(data) == [("answer", "Unknown button.", {"show_alert": True})]
    assert ctl.calls == []


def test_a_session_detail_with_a_plain_dashboard_link_and_back(ctl):
    kind, text, kw = _tap("sessd:2:14")[1]
    assert ctl.calls[0][0] == [CTL, "stats", "--json", "--lane", "2", "--before", "15"]
    assert kind == "edit" and text.startswith("Session #14 · lane 2\nStart: ")
    assert _buttons(kw["reply_markup"]) == [[
        ("Open on dashboard", "https://dash.test/wolfbet/sessions"), ("Back", "sess:2:0")]]


def test_no_dashboard_link_when_the_dashboard_is_not_set(ctl, monkeypatch):
    monkeypatch.setattr(tg_config, "DASHBOARD_URL", "")
    _, _, kw = _tap("sessd:2:14")[1]
    assert _buttons(kw["reply_markup"]) == [[("Back", "sess:2:0")]]


def test_a_session_that_is_gone_says_so(ctl):
    ctl.reply["rows"] = [_session(9, lane=2)]
    _, text, kw = _tap("sessd:2:14")[1]
    assert text == "Session #14 was not found."
    assert _buttons(kw["reply_markup"]) == [[("Back", "sess:2:0")]]


def test_back_returns_to_the_newest_page(ctl):
    _, text, _ = _tap("sess:2:0")[1]
    assert ctl.calls[0][0] == [CTL, "stats", "--json", "--lane", "2"]
    assert text.startswith("#30 · ")
