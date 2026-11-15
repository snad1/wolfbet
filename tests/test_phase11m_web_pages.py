"""Admin pages render against the installed starlette, and the read-only user page note.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11m_web_pages.py -q
"""

import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_HERE)
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from web import config as web_config  # noqa: E402
from web import database as web_db  # noqa: E402
from web.app import app  # noqa: E402
from web.auth import COOKIE_NAME, create_token  # noqa: E402

USER_ID = 111


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_config.settings, "db_path", str(tmp_path / "web.db"))
    data_dir = tmp_path / "botdata"
    user_dir = data_dir / str(USER_ID)
    user_dir.mkdir(parents=True)
    conn = sqlite3.connect(user_dir / "wolfbet.db")
    conn.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, total_bets INTEGER, profit REAL,
                               wagered REAL, highest_balance REAL, highest_win REAL,
                               biggest_loss REAL, max_win_streak INTEGER,
                               max_loss_streak INTEGER);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))

    web_db.init_db()
    web_db.sync_tg_user(USER_ID, "tester")
    jti = web_db.create_web_session("admin", "1", "2099-01-01T00:00:00+00:00")
    test_client = TestClient(app)
    test_client.cookies.set(
        COOKIE_NAME, create_token(subject="admin:1", role="admin", jti=jti)
    )
    return test_client


@pytest.mark.parametrize("path", ["/admin/users", "/admin/audit", "/admin/change-password"])
def test_admin_page_renders(client, path):
    assert client.get(path).status_code == 200


def test_user_page_hides_the_unread_tier_and_block_controls(client):
    page = client.get(f"/admin/users/{USER_ID}")
    assert page.status_code == 200
    assert "tier and blocking take effect" in page.text.lower()
    assert f"/admin/users/{USER_ID}/tier" not in page.text
    assert f"/admin/users/{USER_ID}/block" not in page.text
