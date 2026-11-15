"""Cross-user access control on the JSON API, and the token rules behind it.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11m_web_authz.py -q
"""

import os
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_HERE)
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from web import config as web_config  # noqa: E402
from web import database as web_db  # noqa: E402
from web.auth import COOKIE_NAME, create_token  # noqa: E402
from web.routes import api as api_routes  # noqa: E402

OWNER = 111
OTHER = 222
SESSION_ID = 1

USER_ROUTES = [
    f"/api/users/{OWNER}/sessions",
    f"/api/users/{OWNER}/sessions/csv",
    f"/api/users/{OWNER}/sessions/{SESSION_ID}",
    f"/api/users/{OWNER}/sessions/{SESSION_ID}/bets",
    f"/api/users/{OWNER}/sessions/{SESSION_ID}/bets/csv",
    f"/api/users/{OWNER}/sessions/{SESSION_ID}/chart",
]


def _bot_db(data_dir, user_id):
    user_dir = os.path.join(data_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(user_dir, "wolfbet.db"))
    conn.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, total_bets INTEGER, profit REAL,
                               wins INTEGER, losses INTEGER, strategy TEXT,
                               wagered REAL, started_at TEXT, ended_at TEXT);
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY, session_id INTEGER, timestamp TEXT,
            amount REAL, multiplier REAL, bet_value REAL, rule TEXT,
            result_value REAL, state TEXT, profit REAL, balance_after REAL
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, total_bets, profit) VALUES (1, 0, 0.0)")
    conn.execute(
        "INSERT INTO bets (id, session_id, timestamp, amount, multiplier, state)"
        " VALUES (1, 1, '2026-11-15T10:00:00', 1.0, 2.0, 'win')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_config.settings, "db_path", str(tmp_path / "web.db"))
    data_dir = tmp_path / "botdata"
    _bot_db(data_dir, OWNER)
    _bot_db(data_dir, OTHER)
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))
    web_db.init_db()

    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


def _session_token(subject, role):
    jti = web_db.create_web_session("test", subject, "2099-01-01T00:00:00+00:00")
    return create_token(subject=subject, role=role, jti=jti)


def _as(client, token):
    client.cookies.set(COOKIE_NAME, token)


@pytest.mark.parametrize("path", USER_ROUTES)
def test_owner_reads_own_data(client, path):
    _as(client, _session_token(f"tg:{OWNER}", "tg_user"))
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [p.replace(str(OWNER), str(OTHER)) for p in USER_ROUTES],
)
def test_other_user_is_forbidden(client, path):
    _as(client, _session_token(f"tg:{OWNER}", "tg_user"))
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", USER_ROUTES)
def test_admin_reads_any_user(client, path):
    _as(client, _session_token("admin:1", "admin"))
    assert client.get(path).status_code == 200


def test_token_without_jti_is_rejected(client):
    token = jwt.encode(
        {"sub": f"tg:{OWNER}", "role": "tg_user", "exp": 4102444800},
        web_config.settings.secret_key,
        algorithm="HS256",
    )
    _as(client, token)
    assert client.get(f"/api/users/{OWNER}/sessions").status_code == 401


def test_query_string_token_is_ignored(client):
    token = _session_token(f"tg:{OWNER}", "tg_user")
    response = client.get(f"/api/users/{OWNER}/sessions?token={token}")
    assert response.status_code == 401
