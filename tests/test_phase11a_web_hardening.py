"""Old web startup guards, live session stream and its single poller.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11a_web_hardening.py -q
"""

import asyncio
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_HERE)
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from web import config as web_config  # noqa: E402
from web.routes import ws_routes  # noqa: E402
from web.websocket import manager  # noqa: E402

STRONG_SECRET = "s" * 40
STRONG_PASS = "a-strong-password"
USER_ID = 7
SESSION_ID = 1


@pytest.mark.parametrize("secret_key,admin_pass", [
    ("change-me-to-a-random-64-char-string", STRONG_PASS),
    ("x" * 31, STRONG_PASS),
    (STRONG_SECRET, "changeme123"),
    (STRONG_SECRET, "short-pass1"),
])
def test_startup_refuses_weak_credentials(secret_key, admin_pass):
    with pytest.raises(ValueError):
        web_config.Settings(_env_file=None, secret_key=secret_key, admin_pass=admin_pass)


def test_startup_accepts_strong_credentials():
    settings = web_config.Settings(_env_file=None, secret_key=STRONG_SECRET, admin_pass=STRONG_PASS)
    assert settings.secret_key == STRONG_SECRET


def test_installer_binds_loopback_only():
    script = (Path(_REPO_DIR) / "web" / "install.sh").read_text()
    assert "0.0.0.0" not in script
    assert "admin / changeme123" not in script
    assert "--host 127.0.0.1" in script


@pytest.fixture(autouse=True)
def _clean_ws_state():
    yield
    for task in list(ws_routes._pollers.values()):
        task.cancel()
    ws_routes._pollers.clear()
    manager._channels.clear()


def _make_bot_db(tmp_path: Path) -> Path:
    data_dir = tmp_path / "botdata"
    user_dir = data_dir / str(USER_ID)
    user_dir.mkdir(parents=True)
    db_path = user_dir / "wolfbet.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, total_bets INTEGER, profit REAL);
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY, session_id INTEGER, timestamp TEXT,
            amount REAL, multiplier REAL, bet_value REAL, rule TEXT,
            result_value REAL, state TEXT, profit REAL, balance_after REAL
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, total_bets, profit) VALUES (?, 0, 0.0)", (SESSION_ID,))
    conn.commit()
    conn.close()
    return data_dir


def _insert_bet(db_path: Path, bet_id: int, balance: float = 10.0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bets (id, session_id, timestamp, amount, multiplier, bet_value,"
        " rule, result_value, state, profit, balance_after)"
        " VALUES (?, ?, '2026-11-15T10:00:00', 1.0, 2.0, 1.0, 'over', 2.5, 'win', 1.0, ?)",
        (bet_id, SESSION_ID, balance),
    )
    conn.commit()
    conn.close()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _client(monkeypatch):
    """Minimal app with only the WebSocket router, plus a fixed identity."""

    async def _fake_auth(ws):
        return {"role": "tg_user", "sub": f"tg:{USER_ID}"}

    monkeypatch.setattr(ws_routes, "authenticate_ws", _fake_auth)
    monkeypatch.setattr(ws_routes, "POLL_INTERVAL_SECONDS", 0.02)
    app = FastAPI()
    app.include_router(ws_routes.router)
    return TestClient(app)


def test_live_session_streams_new_bets(tmp_path, monkeypatch):
    data_dir = _make_bot_db(tmp_path)
    db_path = data_dir / str(USER_ID) / "wolfbet.db"
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))
    client = _client(monkeypatch)

    with client:
        with client.websocket_connect(f"/ws/session/{USER_ID}/{SESSION_ID}") as ws:
            assert ws.receive_json()["type"] == "session_update"
            _insert_bet(db_path, 1)
            _insert_bet(db_path, 2)
            seen = []
            while len(seen) < 2:
                msg = ws.receive_json()
                if msg["type"] == "new_bet":
                    seen.append(msg["data"]["time"])
            assert seen == [1, 2]


def test_two_clients_share_one_session_poller(tmp_path, monkeypatch):
    data_dir = _make_bot_db(tmp_path)
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))

    gate = threading.Event()
    calls = []

    async def _gated(user_id, session_id, after_id):
        calls.append(after_id)
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return []

    monkeypatch.setattr(ws_routes, "_fetch_new_bets", _gated)
    client = _client(monkeypatch)

    with client:
        with client.websocket_connect(f"/ws/session/{USER_ID}/{SESSION_ID}"):
            assert _wait_for(lambda: len(calls) >= 1), "first poller never reached the DB read"
            with client.websocket_connect(f"/ws/session/{USER_ID}/{SESSION_ID}"):
                time.sleep(0.3)
                assert len(calls) == 1, f"expected one poller, saw {len(calls)} fetches"
                gate.set()
            time.sleep(0.2)
        assert _wait_for(lambda: not ws_routes._pollers), "poller outlived the last subscriber"
        settled = len(calls)
        time.sleep(0.2)
        assert len(calls) == settled, "poller kept polling after the last subscriber left"


