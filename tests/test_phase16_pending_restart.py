"""A running lane shows the saved changes it takes only on a restart; a session keeps its config."""

import json
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402

FAKE_KEY = "wb_live_SECRET_0123456789abcdef"


@pytest.fixture
def lane(monkeypatch, tmp_path):
    """A lane 1 process running on multiplier 3 and max_loss 0.02, with its files in tmp."""
    config = tmp_path / "config.json"
    monkeypatch.setattr(wb, "CONFIG_PATH", str(config))
    monkeypatch.setattr(wb, "STATE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr(wb, "_BASE_STATE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr(wb, "state", wb.BotState())
    wb.state.lane, wb.state.multiplier, wb.state.max_loss = 1, 3.0, 0.02
    wb.state.proxy = "http://user:secret-pw@10.0.0.2:3128"

    def save(**values):
        config.write_text(json.dumps({"multiplier": 3.0, "max_loss": 0.02, **values}))

    save()
    return save


def _running_state_file(pid=None):
    data = wb._state_snapshot()
    data["pid"] = os.getpid() if pid is None else pid
    with open(wb.STATE_PATH, "w") as f:
        json.dump(data, f)


def test_a_restart_only_change_is_pending_and_a_live_one_applies(lane):
    lane(multiplier=4.0, max_loss=0.01)
    wb._reload_live_config()
    assert wb.state.max_loss == 0.01
    assert wb.state.pending_restart == {"multiplier": [3.0, 4.0]}
    _running_state_file()
    assert wb._pending_restarts() == {"1": [{"key": "multiplier", "running": 3.0, "saved": 4.0}]}


def test_a_change_set_back_is_no_longer_pending(lane):
    lane(multiplier=4.0)
    wb._reload_live_config()
    lane()
    wb._reload_live_config()
    assert wb.state.pending_restart == {}


def test_a_stopped_lane_has_nothing_pending(lane):
    lane(multiplier=4.0)
    wb._reload_live_config()
    _running_state_file(pid=2**22 + 12345)
    assert wb._pending_restarts() == {}


def test_secrets_never_reach_the_pending_list(lane):
    lane(api_key=FAKE_KEY)
    wb._reload_live_config()
    assert "api_key" not in wb.state.pending_restart
    assert "SECRET" not in json.dumps(wb.state.pending_restart)


def test_a_session_row_keeps_the_settings_it_started_with(lane, monkeypatch, tmp_path):
    db = str(tmp_path / "bot.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    monkeypatch.setattr(wb, "_emit_lifecycle", lambda *a, **k: None)
    sid = wb._db_start_session()
    with sqlite3.connect(db) as conn:
        (raw,) = conn.execute("SELECT config_snapshot FROM sessions WHERE id = ?", (sid,)).fetchone()
    snapshot = json.loads(raw)
    assert snapshot["multiplier"] == 3.0 and snapshot["max_loss"] == 0.02
    assert snapshot["proxy"] == "http://10.0.0.2:3128"
    assert "secret-pw" not in raw and "api_key" not in snapshot
