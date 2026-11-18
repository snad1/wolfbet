"""Always-run guard for the bet hot path: emitting never waits on Redis, and events off never loads redis.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11f_bench_guard.py -q
"""

import gc
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from core.events import EventPublisher  # noqa: E402

_BET = (1, 0.0001, 2.0, "over", 49.5, 75.0, "win", 0.0001, 1000.0001, 0.0001, 1, 1, 30.0)


class _StuckRedis:
    def __init__(self):
        self.release = threading.Event()
        self.blocked = threading.Event()

    def pipeline(self, transaction=True):
        return self

    def xadd(self, *args, **kwargs):
        pass

    def execute(self):
        self.blocked.set()
        self.release.wait(10)


def test_emit_stays_under_budget_while_the_sender_is_stuck():
    client = _StuckRedis()
    pub = EventPublisher("redis://unused", client=client)
    pub.start()
    try:
        pub.emit_bet(_BET)
        assert client.blocked.wait(2)
        t0 = time.perf_counter()
        for _ in range(100_000):
            pub.emit_bet(_BET)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        client.release.set()
        pub.stop(timeout=2)
    assert elapsed_ms < 100, f"100k emits took {elapsed_ms:.0f} ms"


def test_queued_bets_leave_nothing_for_the_garbage_collector():
    client = _StuckRedis()
    pub = EventPublisher("redis://unused", client=client)
    pub.start()
    try:
        pub.emit_bet(_BET)
        assert client.blocked.wait(2)
        gc.collect()
        gc.disable()
        try:
            before = gc.get_count()[0]
            for _ in range(10_000):
                pub.emit_bet(_BET)
            grown = gc.get_count()[0] - before
        finally:
            gc.enable()
    finally:
        client.release.set()
        pub.stop(timeout=2)
    assert grown < 100, f"{grown} GC-tracked objects left alive by 10k queued bets"


def test_events_off_never_loads_redis(tmp_path):
    env = dict(os.environ, APP_ENV="testing")
    for attr in ("DB_PATH", "CONFIG_PATH", "STATE_PATH", "PID_PATH", "RUN_PATH",
                 "PRESET_PATH", "LOG_DIR"):
        env[f"WOLFBET_{attr}"] = str(tmp_path / attr.lower())
    env.pop("WOLFBET_EVENTS_URL", None)
    code = (
        "import sys, threading; sys.path.insert(0, sys.argv[1]);"
        "import wolfbet as wb;"
        "wb._start_events(); wb._events_tick(); wb._emit_session_end(); wb._stop_events();"
        "assert wb._event_publisher is None, 'publisher started';"
        "assert 'redis' not in sys.modules, 'redis imported';"
        "assert not any(t.name == 'EventPublisher' for t in threading.enumerate()), 'thread'"
    )
    result = subprocess.run([sys.executable, "-c", code, _WOLFBET_DIR],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr[-500:]
