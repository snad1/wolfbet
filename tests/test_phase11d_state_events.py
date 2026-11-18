"""State and lifecycle events: status every 2 s, pause/resume, reload, recurring cycles, start and stop.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11d_state_events.py -q
"""

import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from core.events import LIFECYCLE_KINDS, EventPublisher  # noqa: E402

_VOLATILE = {"updated_at", "uptime_sec", "run_uptime_sec", "pause_remaining_sec"}


class _Recorder:
    """Stands in for the publisher and keeps every emit in order."""

    def __init__(self):
        self.calls = []

    def emit_state(self, state):
        self.calls.append(("state", state))

    def emit_lifecycle(self, kind, data):
        assert kind in LIFECYCLE_KINDS
        json.dumps(data)
        self.calls.append((kind, data))

    def emit_bet(self, fields):
        self.calls.append(("bet", fields))

    def stats(self):
        return {"queued": 0, "sent": 0, "dropped": 0, "overflow": 0,
                "last_error": "", "connected": True}

    def kinds(self):
        return [k for k, _ in self.calls]


class _FakePipe:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def xadd(self, stream, fields, maxlen=None, approximate=False):
        self._ops.append((stream, fields["data"]))

    def execute(self):
        self._client.entries.extend(self._ops)


class _FakeRedis:
    def __init__(self):
        self.entries = []

    def pipeline(self, transaction=True):
        return _FakePipe(self)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    for attr in ("DB_PATH", "STATE_PATH", "CONFIG_PATH", "RUN_PATH"):
        monkeypatch.setattr(wb, attr, str(tmp_path / attr.lower()))
    monkeypatch.setattr(wb, "_events_next_state", 0.0)
    monkeypatch.setattr(wb, "_events_last_paused", False)
    monkeypatch.setattr(wb._http_session, "proxies", {})
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    s = wb.state
    saved = {k: v for k, v in vars(s).items() if k != "lock"}
    wb.init_db()
    s.session_id = wb._db_start_session()
    s.paused = False
    s._insufficient_balance = False
    rec = _Recorder()
    monkeypatch.setattr(wb, "_event_publisher", rec)
    yield rec
    for key in [k for k in vars(s) if k != "lock" and k not in saved]:
        delattr(s, key)
    for key, value in saved.items():
        setattr(s, key, value)


def test_state_file_is_the_snapshot(bot):
    written = wb._save_state_file()
    with open(wb.STATE_PATH) as f:
        on_disk = json.load(f)
    snapshot = wb._state_snapshot()
    strip = lambda d: {k: v for k, v in d.items() if k not in _VOLATILE}  # noqa: E731
    assert strip(on_disk) == strip(written) == strip(json.loads(json.dumps(snapshot)))


def test_state_goes_out_every_two_seconds_without_secrets(bot, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    marker = "leak-check-" + "marker"
    setattr(wb.state, "api_key", marker)
    wb._events_tick()
    wb._events_tick()
    clock[0] += 2.0
    wb._events_tick()

    states = [d for k, d in bot.calls if k == "state"]
    assert len(states) == 2
    assert set(states[0]) == set(wb._state_snapshot())
    assert "api_key" not in states[0] and "proxy" not in states[0]
    assert marker not in json.dumps(states[0])


def test_pause_and_resume_are_sent_once_per_change(bot):
    wb.state.paused = True
    wb._events_tick()
    wb._events_tick()
    wb.state.paused = False
    wb._events_tick()
    assert [k for k in bot.kinds() if k in ("pause", "resume")] == ["pause", "resume"]


def test_reload_sends_applied_fields_with_proxy_masked(bot):
    wb.state.max_bets = None
    with open(wb.CONFIG_PATH, "w") as f:
        json.dump({"proxy": "http://user:s3cret@1.2.3.4:8080", "max_bets": 50}, f)
    wb._sig_reload(None, None)

    reloads = [d for k, d in bot.calls if k == "reload"]
    assert len(reloads) == 1
    applied = reloads[0]["applied"]
    assert applied["max_bets"] == [None, 50]
    assert "proxy" in applied
    assert "s3cret" not in json.dumps(reloads[0])


def test_recurring_restart_closes_cycles_and_opens(bot):
    s = wb.state
    old_id = s.session_id
    s.total_bets, s.wins, s.losses, s.profit = 12, 8, 4, 0.0004
    wb._reset_session_for_recurring(0, "Profit target reached: +0.0004")

    kinds = [k for k in bot.kinds() if k != "state"]
    assert kinds == ["session_close", "cycle", "session_open"]
    close, cycle, opened = (d for k, d in bot.calls if k != "state")
    assert close["session_id"] == old_id and close["total_bets"] == 12
    assert close["profit"] == pytest.approx(0.0004)
    assert cycle == {"from_session_id": old_id, "reason": "Profit target reached: +0.0004",
                     "delay_s": 0}
    assert opened["session_id"] == s.session_id != old_id


def test_start_events_announces_start_and_current_session(bot, monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(wb, "_event_publisher", None)
    monkeypatch.setattr(wb, "EventPublisher",
                        lambda url, lane: EventPublisher(url, lane=lane, client=client))
    wb.state.events_enabled = True
    wb.state.events_url = "redis://unused"
    wb.state.lane = 2
    wb._start_events()
    wb._stop_events()

    events = [json.loads(data) for _, data in client.entries]
    assert [e["payload"]["kind"] for e in events] == ["start", "session_open"]
    start = events[0]["payload"]["data"]
    assert start["version"] == wb.VERSION and start["lane"] == 2
    assert events[1]["payload"]["data"]["session_id"] == wb.state.session_id
    assert all(e["lane"] == 2 for e in events)


@pytest.mark.parametrize("insufficient, expected", [
    (True, ["error", "session_close", "stop"]),
    (False, ["session_close", "stop"]),
])
def test_session_end_events(bot, insufficient, expected):
    wb.state._insufficient_balance = insufficient
    wb.state.status = "STOPPED: Insufficient balance" if insufficient else "STOPPED: Max bets reached: 10"
    wb._emit_session_end()
    assert bot.kinds() == expected
    assert bot.calls[-1][1] == {"reason": wb.state.status}


def test_everything_is_a_no_op_without_a_publisher(bot, monkeypatch):
    monkeypatch.setattr(wb, "_event_publisher", None)
    wb.state.paused = True
    wb._events_tick()
    wb._emit_session_end()
    wb._reset_session_for_recurring(0, "x")
    wb._sig_reload(None, None)
    assert bot.calls == []
