"""Telegram owner gate, update launcher argv, and handlers off the event loop.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11n_tg_hardening.py -q
"""

import asyncio
import inspect
import os
import sqlite3
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

from tg import handlers as tg_handlers  # noqa: E402
from tg.database import execute, fetch_all, fetch_one  # noqa: E402

OWNER_ID = 4242


def _update(user_id=OWNER_ID):
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
        return types.SimpleNamespace(edit_text=edit_text)

    async def edit_text(text, **kw):
        sent.append(text)

    msg = types.SimpleNamespace(reply_text=reply_text, edit_text=edit_text)
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id),
        message=msg,
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=1),
    ), sent


class TestOwnerGate:
    def test_unset_env_owns_nobody(self, monkeypatch):
        monkeypatch.delenv("TG_OWNER_ID", raising=False)
        assert tg_handlers._is_owner(OWNER_ID) is False
        assert tg_handlers._is_owner(680974641) is False

    def test_non_numeric_env_owns_nobody(self, monkeypatch):
        monkeypatch.setenv("TG_OWNER_ID", "not-a-number")
        assert tg_handlers._is_owner(OWNER_ID) is False

    def test_only_the_configured_id(self, monkeypatch):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        assert tg_handlers._is_owner(OWNER_ID) is True
        assert tg_handlers._is_owner(OWNER_ID + 1) is False


class TestUpdateArgv:
    def test_argv_has_no_shell(self):
        argv = tg_handlers._update_argv("/usr/local/bin/wolfctl", "/root/wolfbet", "/data/update.log")
        assert argv[0] == "systemd-run"
        assert "--user" in argv
        assert "bash" not in argv
        assert "-c" not in argv
        assert "sh" not in argv
        assert argv[-2:] == ["/usr/local/bin/wolfctl", "update"]

    def test_every_value_is_its_own_element(self):
        argv = tg_handlers._update_argv("/usr/local/bin/wolfctl", "/root/wolfbet", "/data/update.log")
        assert "--working-directory=/root/wolfbet" in argv
        assert "--property=StandardOutput=append:/data/update.log" in argv
        assert "--property=StandardError=append:/data/update.log" in argv
        assert any(a.startswith("--on-active=") for a in argv)

    def test_a_hostile_path_stays_one_argument(self):
        hostile = "/root/wolfbet; touch /tmp/pwned"
        argv = tg_handlers._update_argv("/usr/local/bin/wolfctl", hostile, "/data/update.log")
        assert f"--working-directory={hostile}" in argv
        assert not any(a == "touch" for a in argv)


class TestUpdateRefusals:
    def _run(self, monkeypatch, user_id=OWNER_ID):
        launched = []

        def _fake_run(cmd, **kw):
            launched.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tg_handlers.subprocess, "run", _fake_run)
        upd, sent = _update(user_id)
        asyncio.run(tg_handlers.cmd_update(upd, types.SimpleNamespace(args=[])))
        return sent, launched

    def test_other_user_is_refused(self, monkeypatch):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        sent, launched = self._run(monkeypatch, user_id=OWNER_ID + 1)
        assert sent == ["Owner only."]
        assert launched == []

    def test_unset_env_refuses_even_the_old_default_id(self, monkeypatch):
        monkeypatch.delenv("TG_OWNER_ID", raising=False)
        sent, launched = self._run(monkeypatch, user_id=680974641)
        assert sent == ["Owner only."]
        assert launched == []

    def test_missing_ctl_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        monkeypatch.setattr(tg_handlers.shutil, "which", lambda name: None)
        monkeypatch.setattr(tg_handlers, "REPO_DIR", str(tmp_path))
        sent, launched = self._run(monkeypatch)
        assert launched == []
        assert "not found in PATH" in sent[0]

    def test_relative_repo_dir_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        ctl = tmp_path / "wolfctl"
        ctl.write_text("#!/bin/sh\n")
        ctl.chmod(0o755)
        monkeypatch.setattr(tg_handlers.shutil, "which", lambda name: str(ctl))
        monkeypatch.setattr(tg_handlers, "REPO_DIR", "relative/dir")
        sent, launched = self._run(monkeypatch)
        assert launched == []
        assert "not configured correctly" in sent[0]

    def test_owner_launches_with_argv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        ctl = tmp_path / "wolfctl"
        ctl.write_text("#!/bin/sh\n")
        ctl.chmod(0o755)
        repo = tmp_path / "repo"
        repo.mkdir()
        log = tmp_path / "update.log"
        monkeypatch.setattr(
            tg_handlers.shutil, "which",
            lambda name: str(ctl) if name == "wolfctl" else "/usr/bin/systemd-run",
        )
        monkeypatch.setattr(tg_handlers, "REPO_DIR", str(repo))
        monkeypatch.setattr(tg_handlers, "_prepare_update_log", lambda: str(log))
        sent, launched = self._run(monkeypatch)
        assert len(launched) == 1
        assert launched[0][0] == "systemd-run"
        assert any(str(log) in arg for arg in launched[0])


class TestBenchmarkDoesNotBlockTheLoop:
    def test_loop_ticks_during_the_run(self, monkeypatch):
        ticks = 0

        async def slow_post(client, url, payload):
            await asyncio.sleep(0.02)
            return types.SimpleNamespace(status_code=200)

        monkeypatch.setattr(tg_handlers, "_benchmark_post", slow_post)
        monkeypatch.setattr(
            tg_handlers, "load_user_config",
            lambda uid: {"api_key": "k", "currency": "btc", "multiplier": 2.0},
        )
        upd, sent = _update()

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        async def run():
            task = asyncio.create_task(ticker())
            await tg_handlers.cmd_benchmark(upd, types.SimpleNamespace(args=["5"]))
            task.cancel()

        asyncio.run(run())
        assert ticks > 0
        assert any("Benchmark Results" in text for text in sent)

    def test_benchmark_does_not_use_the_sync_http_client(self):
        source = inspect.getsource(tg_handlers.cmd_benchmark)
        assert "import requests" not in source
        assert "session.post" not in source


class TestAsyncDatabaseHelpers:
    def _db(self, tmp_path):
        path = str(tmp_path / "wolfbet.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE bets (id INTEGER PRIMARY KEY, state TEXT, profit REAL)")
        conn.execute("INSERT INTO bets (id, state, profit) VALUES (1, 'win', 0.5)")
        conn.execute("INSERT INTO bets (id, state, profit) VALUES (2, 'loss', -0.2)")
        conn.commit()
        conn.close()
        return path

    def test_fetch_all(self, tmp_path):
        rows = asyncio.run(fetch_all(self._db(tmp_path), "SELECT * FROM bets ORDER BY id"))
        assert [r[1] for r in rows] == ["win", "loss"]

    def test_fetch_one(self, tmp_path):
        row = asyncio.run(fetch_one(self._db(tmp_path), "SELECT state FROM bets WHERE id = ?", (2,)))
        assert row[0] == "loss"

    def test_execute_reports_row_count(self, tmp_path):
        path = self._db(tmp_path)
        changed = asyncio.run(execute(path, "UPDATE bets SET state = 'win'"))
        assert changed == 2
