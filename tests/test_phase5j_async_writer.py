"""Phase 5j tests — async bet-writer + SQLite PRAGMA tuning.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5j_async_writer.py -q
"""

import os
import queue as _queue_mod
import sqlite3
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_state_and_writer_stats():
    """Fresh per-test state — new tmp DB path swapped in, writer thread stopped.

    Returns the tmp DB path (caller owns cleanup)."""
    fd, path = tempfile.mkstemp(prefix="wb_async_", suffix=".db")
    os.close(fd)
    os.remove(path)
    wb.DB_PATH = path
    wb.init_db()
    wb.state.session_id = 1   # match sessions FK
    wb.state.rule = "over"
    wb.state.db_write_mode = "async"
    # Reset shared writer primitives.
    wb._bet_writer_stop.set()
    if wb._bet_writer_thread is not None and wb._bet_writer_thread.is_alive():
        wb._bet_writer_thread.join(timeout=5.0)
    wb._bet_writer_thread = None
    with wb._bet_write_queue.mutex:
        wb._bet_write_queue.queue.clear()
    for k in wb._bet_writer_stats:
        wb._bet_writer_stats[k] = 0
    wb._bet_writer_stop.clear()
    # Insert a sessions row so FK constraint holds if enabled.
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
        "multiplier, start_balance) VALUES (?,?,?,?,?,?,?,?)",
        (1, "2026-07-18T20:00:00Z", "usdt", "dice", "Flat", 0.0001, 2.0, 1.0),
    )
    conn.commit()
    conn.close()
    return path


def _teardown(path):
    wb._bet_writer_stop.set()
    if wb._bet_writer_thread is not None and wb._bet_writer_thread.is_alive():
        wb._bet_writer_thread.join(timeout=5.0)
    wb._bet_writer_thread = None
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _fake_bet(state="win", amount=0.0001, profit=0.0001):
    return {
        "amount": amount,
        "multiplier": 2.0,
        "bet_value": 50.0,
        "result_value": 60.0,
        "state": state,
        "profit": profit,
    }


# ── Config membership ────────────────────────────────────
def test_default_db_write_mode_async():
    assert wb.state.db_write_mode in ("async", "sync")


def test_db_write_mode_in_config_keys():
    assert "db_write_mode" in wb._CONFIG_KEYS


def test_db_write_mode_in_preset_keys():
    assert "db_write_mode" in wb._PRESET_KEYS


def test_db_write_mode_in_tweak_params():
    assert "db_write_mode" in wb._TWEAK_PARAMS


def test_alias_dbmode():
    assert wb._TWEAK_ALIAS.get("dbmode") == "db_write_mode"


def test_db_write_mode_in_show_fields():
    assert any(f[0] == "db_write_mode" for f in wb._CONFIG_SHOW_FIELDS)


# ── _apply_mutation ──────────────────────────────────────
def test_apply_mutation_async():
    wb._apply_mutation({"db_write_mode": "async"})
    assert wb.state.db_write_mode == "async"


def test_apply_mutation_sync():
    wb._apply_mutation({"db_write_mode": "sync"})
    assert wb.state.db_write_mode == "sync"


def test_apply_mutation_rejects_unknown():
    try:
        wb._apply_mutation({"db_write_mode": "loud"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown db_write_mode")


# ── _open_bet_writer_conn — PRAGMAs applied ───────────────
def test_open_bet_writer_conn_sets_pragmas():
    path = _reset_state_and_writer_stats()
    try:
        conn = wb._open_bet_writer_conn()
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        # NORMAL is integer 1 in SQLite; some builds return string — accept both.
        assert sync in (1, "1", "NORMAL")
        assert int(bt) == 5000
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert int(limit) == wb._WAL_SIZE_LIMIT_BYTES
        conn.close()
    finally:
        _teardown(path)


def test_wal_is_cut_back_to_the_limit_after_a_checkpoint(monkeypatch):
    path = _reset_state_and_writer_stats()
    monkeypatch.setattr(wb, "_WAL_SIZE_LIMIT_BYTES", 256 * 1024)
    try:
        conn = wb._open_bet_writer_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS ballast (data BLOB)")
        conn.executemany("INSERT INTO ballast VALUES (?)", [(b"x" * 4096,)] * 1000)
        conn.commit()
        assert os.path.getsize(path + "-wal") > 1024 * 1024
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.execute("INSERT INTO ballast VALUES (x'00')")
        conn.commit()
        assert os.path.getsize(path + "-wal") <= 256 * 1024
        conn.close()
    finally:
        _teardown(path)


# ── End-to-end enqueue + writer flush ────────────────────
def _wait_for_flush(target_count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wb._bet_writer_stats["flushed"] >= target_count:
            return True
        time.sleep(0.02)
    return False


def test_async_write_lands_single_row():
    path = _reset_state_and_writer_stats()
    try:
        wb._start_bet_writer()
        wb._db_save_bet_async(_fake_bet(), profit=0.0001, balance=1.0001)
        assert _wait_for_flush(1), "writer did not flush row within 5s"
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 1
    finally:
        _teardown(path)


def test_async_write_batches_200_rows():
    path = _reset_state_and_writer_stats()
    try:
        wb._start_bet_writer()
        N = 200
        for i in range(N):
            wb._db_save_bet_async(_fake_bet(profit=0.0001 * i), profit=0.0001 * i, balance=1.0)
        assert _wait_for_flush(N, timeout=10.0), (
            f"only {wb._bet_writer_stats['flushed']}/{N} flushed")
        # Batches should be > 1 (batch size 32).
        assert wb._bet_writer_stats["batches"] >= 2
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == N
    finally:
        _teardown(path)


# ── Queue overflow → sync fallback ───────────────────────
def test_queue_full_triggers_sync_fallback():
    path = _reset_state_and_writer_stats()
    try:
        # DO NOT start the writer thread — the queue will fill up.
        # Fill the queue up to maxsize.
        for _ in range(wb._bet_write_queue.maxsize):
            wb._bet_write_queue.put_nowait(("stub",) * 10)
        # Next call should overflow → fallback_sync path.
        wb._db_save_bet_async(_fake_bet(), profit=0.0001, balance=1.0001)
        assert wb._bet_writer_stats["fallback_sync"] >= 1
        # Row landed via inline sync path.
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 1
    finally:
        # Drain the stub rows so subsequent tests start clean.
        with wb._bet_write_queue.mutex:
            wb._bet_write_queue.queue.clear()
        _teardown(path)


# ── Shutdown drain ───────────────────────────────────────
def test_stop_bet_writer_drains_pending():
    path = _reset_state_and_writer_stats()
    try:
        wb._start_bet_writer()
        for _ in range(10):
            wb._db_save_bet_async(_fake_bet(), profit=0.0001, balance=1.0)
        wb._stop_bet_writer(timeout=5.0)
        assert wb._bet_writer_stats["flushed"] == 10
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 10
    finally:
        _teardown(path)


# ── Dispatcher routing ────────────────────────────────────
def test_dispatch_sync_when_mode_is_sync():
    path = _reset_state_and_writer_stats()
    try:
        wb.state.db_write_mode = "sync"
        wb._db_save_bet_dispatch(_fake_bet(), 0.0001, 1.0)
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 1
        # Async counters should NOT have moved.
        assert wb._bet_writer_stats["queued"] == 0
        assert wb._bet_writer_stats["flushed"] == 0
    finally:
        _teardown(path)


def test_dispatch_async_when_mode_is_async_and_writer_running():
    path = _reset_state_and_writer_stats()
    try:
        wb.state.db_write_mode = "async"
        wb._start_bet_writer()
        wb._db_save_bet_dispatch(_fake_bet(), 0.0001, 1.0)
        assert _wait_for_flush(1)
        assert wb._bet_writer_stats["queued"] >= 1
    finally:
        _teardown(path)


def test_dispatch_falls_back_to_sync_when_writer_not_started():
    path = _reset_state_and_writer_stats()
    try:
        # writer is NOT started; async mode set → dispatcher must fall through to sync.
        wb.state.db_write_mode = "async"
        wb._bet_writer_thread = None
        wb._db_save_bet_dispatch(_fake_bet(), 0.0001, 1.0)
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 1
    finally:
        _teardown(path)


# ── _make_bet_row shape ──────────────────────────────────
def test_make_bet_row_10_elements():
    row = wb._make_bet_row(_fake_bet(), 0.0001, 1.0001)
    assert len(row) == 10
    # timestamp is index 1 — must be an ISO string.
    assert isinstance(row[1], str)
