"""Phase 9 tests — streak reset (restart on first win after N+ losses in a row).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase9_streak_reset.py -q
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import pytest  # noqa: E402

import wolfbet as wb  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    s = wb.state
    s.game = "dice"
    s.strategy_key = "1"
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.current_balance = 1.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.recurring = False
    s.recurring_delay = 60.0
    s.streak_reset_losses = None
    s.streak_reset_delay = 600.0
    s.profit_threshold = None
    s.session_id = 42
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.current_streak = 0
    s.last_broken_loss_streak = 0
    s.session_pause_total = 0.0
    s._auto_uuid = None
    monkeypatch.setattr(wb, "_db_start_session", lambda: 43)


def _play(results, profit_after):
    """Simulate a run of 'win'/'loss' outcomes through the streak bookkeeping."""
    s = wb.state
    for r in results:
        if r == "win":
            s.last_broken_loss_streak = max(-s.current_streak, 0)
            s.current_streak = max(s.current_streak, 0) + 1
        else:
            s.current_streak = min(s.current_streak, 0) - 1
    s.profit = profit_after


def test_defaults():
    s = wb.WolfState() if hasattr(wb, "WolfState") else None
    if s is None:
        s = type(wb.state)()
    assert s.streak_reset_losses is None
    assert s.streak_reset_delay == 600.0


def test_fires_on_win_after_n_losses_at_profit():
    wb.state.streak_reset_losses = 20
    _play(["loss"] * 22 + ["win"], profit_after=0.0001)
    stop, reason = wb.should_stop()
    assert stop
    assert reason.startswith("Loss streak reset")
    assert "22 losses" in reason
    # Restarts even with recurring off.
    assert wb.state.recurring is False
    assert wb._should_recurring_restart(reason)
    assert wb._restart_delay_for(reason) == 600.0


def test_no_fire_when_in_red():
    wb.state.streak_reset_losses = 20
    _play(["loss"] * 20 + ["win"], profit_after=-0.5)
    assert wb.should_stop() == (False, "")


def test_no_fire_below_threshold():
    wb.state.streak_reset_losses = 20
    _play(["loss"] * 19 + ["win"], profit_after=0.1)
    assert wb.should_stop() == (False, "")


def test_no_fire_when_off():
    _play(["loss"] * 30 + ["win"], profit_after=0.1)
    assert wb.should_stop() == (False, "")


def test_only_the_breaking_win_fires():
    wb.state.streak_reset_losses = 20
    _play(["loss"] * 20 + ["win", "win"], profit_after=0.1)
    assert wb.should_stop() == (False, "")


def test_red_win_then_later_does_not_fire():
    """In the red at the breaking win → betting continues; trigger disarms."""
    wb.state.streak_reset_losses = 20
    _play(["loss"] * 20 + ["win"], profit_after=-0.1)
    assert wb.should_stop() == (False, "")
    _play(["win"], profit_after=0.1)
    assert wb.should_stop() == (False, "")


def test_reset_uses_streak_reset_delay(monkeypatch):
    slept = []
    monkeypatch.setattr(wb.time, "sleep", lambda d: slept.append(d))
    wb.state.streak_reset_losses = 20
    wb.state.streak_reset_delay = 123.0
    _play(["loss"] * 20 + ["win"], profit_after=0.1)
    _, reason = wb.should_stop()
    wb._reset_session_for_recurring(wb._restart_delay_for(reason))
    assert slept == [123.0]
    assert wb.state.session_pause_total == 123.0
    assert wb.state.last_broken_loss_streak == 0
    assert wb.state.current_streak == 0
    assert wb.state.session_id == 43


def test_other_reasons_keep_recurring_delay():
    assert wb._restart_delay_for("Profit target reached: +1") == 60.0


def test_reason_ignored_when_feature_off():
    assert not wb._should_recurring_restart("Loss streak reset: 20 losses then win")


def test_mutation_aliases_and_off():
    changes = {wb._TWEAK_ALIAS.get(k, k): v for k, v in {"srloss": "20", "srdelay": "900"}.items()}
    wb._apply_mutation(changes)
    assert wb.state.streak_reset_losses == 20
    assert wb.state.streak_reset_delay == 900.0
    wb._apply_mutation({"streak_reset_losses": "off", "streak_reset_delay": "off"})
    assert wb.state.streak_reset_losses is None
    assert wb.state.streak_reset_delay == 0.0


def test_mutation_rejects_bad_values():
    with pytest.raises(ValueError):
        wb._apply_mutation({"streak_reset_losses": "-3"})
    with pytest.raises(ValueError):
        wb._apply_mutation({"streak_reset_losses": "abc"})
    msgs = wb._apply_mutation({"streak_reset_delay": "999999"})
    assert wb.state.streak_reset_delay == 86400.0
    assert any("clamped" in m for m in msgs)


def test_keys_persisted_and_live_reloadable():
    for k in ("streak_reset_losses", "streak_reset_delay"):
        assert k in wb._CONFIG_KEYS
        assert k in wb._PRESET_KEYS
        assert k in wb._LIVE_RELOAD_KEYS
        assert k in wb._TWEAK_PARAMS


def test_pipelined_outcome_records_broken_streak():
    """_apply_outcome_locked must capture the broken loss run like the sequential loop."""
    import inspect
    src = inspect.getsource(wb._apply_outcome_locked)
    assert "last_broken_loss_streak" in src


# ── TG engine + handlers ─────────────────────────────────
def _tg_engine(cfg):
    from tg.engine import BettingEngine
    return BettingEngine(1, ":memory:", "k" * 40, {"game": "dice", **cfg})


def _tg_play(eng, results, win_profit=0.4):
    for st in results:
        eng._process_bet_result({
            "bet": {"state": st, "amount": 0.1,
                    "profit": win_profit if st == "win" else -0.1},
            "userBalance": {"amount": 1.0},
        })


def test_tg_engine_fires_on_breaking_win():
    eng = _tg_engine({"streak_reset_losses": 3})
    _tg_play(eng, ["loss"] * 3 + ["win"])
    stop, reason = eng._should_stop()
    assert stop and reason == "Loss streak reset: 3 losses then win"


def test_tg_engine_no_fire_in_red_or_below_threshold():
    eng = _tg_engine({"streak_reset_losses": 3})
    _tg_play(eng, ["loss"] * 3 + ["win"], win_profit=0.1)  # profit -0.2
    assert eng._should_stop() == (False, "")
    eng = _tg_engine({"streak_reset_losses": 3})
    _tg_play(eng, ["loss"] * 2 + ["win"])
    assert eng._should_stop() == (False, "")


def test_tg_engine_mutate_and_snapshot():
    import asyncio
    eng = _tg_engine({})
    assert eng.streak_reset_losses is None
    asyncio.run(eng.mutate({"streak_reset_losses": 20}))
    assert eng.streak_reset_losses == 20


def test_tg_streak_reset_delay_helper():
    import tg.handlers as h
    assert h._streak_reset_delay({}) == 600
    assert h._streak_reset_delay({"streak_reset_delay": 0}) == 5
    assert h._streak_reset_delay({"streak_reset_delay": 900}) == 900


def test_tg_config_persists_via_shared(tmp_path):
    """casino-shared stores the whole config dict — both keys round-trip."""
    from shared.tg.database import load_user_config, save_user_config
    cfg = {"recurring": False, "streak_reset_losses": 20, "streak_reset_delay": 600}
    save_user_config(str(tmp_path), 7, cfg)
    assert load_user_config(str(tmp_path), 7) == cfg


def _run_notify_stop(monkeypatch, tmp_path, live_cfg, reason):
    """Drive tg.handlers._notify_stop with the real shared config store; return restart calls."""
    import asyncio
    from types import SimpleNamespace
    import tg.handlers as h
    from shared.tg.database import load_user_config, save_user_config

    uid, slot = 7, 1
    save_user_config(str(tmp_path), uid, live_cfg)
    monkeypatch.setattr(h, "load_user_config", lambda u: load_user_config(str(tmp_path), u))

    async def _send(*a, **k):
        return None
    monkeypatch.setattr(h, "_safe_send", _send)

    calls = []

    async def _restart(chat_id, user_id, old_slot, delay, config, app):
        calls.append((delay, dict(config)))
    monkeypatch.setattr(h, "_recurring_restart", _restart)

    engine = SimpleNamespace(profit=0.1, base_bet=0.0001, session_id=1,
                             get_status=lambda: {})
    monkeypatch.setitem(h.active_engines, uid, {slot: engine})
    monkeypatch.setitem(h.recurring_state, uid, {slot: {
        "delay": 60, "chat_id": 1, "config": {"streak_reset_losses": 5},
        "timer_task": None, "app": None,
    }})

    async def _go():
        await h._notify_stop(1, uid, slot, reason, None)
        await asyncio.sleep(0)  # let the scheduled restart task run
    asyncio.run(_go())
    return calls


def test_tg_notify_stop_streak_reset_restarts_without_recurring(monkeypatch, tmp_path):
    calls = _run_notify_stop(
        monkeypatch, tmp_path,
        {"recurring": False, "streak_reset_losses": 20, "streak_reset_delay": 900},
        "Loss streak reset: 20 losses then win")
    assert len(calls) == 1
    delay, cfg = calls[0]
    assert delay == 900
    assert cfg["streak_reset_losses"] == 20  # live /set value carried forward
    assert cfg["streak_reset_delay"] == 900


def test_tg_notify_stop_streak_reset_off_no_restart(monkeypatch, tmp_path):
    calls = _run_notify_stop(
        monkeypatch, tmp_path,
        {"recurring": True, "streak_reset_losses": None},
        "Loss streak reset: 20 losses then win")
    assert calls == []


def test_tg_notify_stop_recurring_keeps_recurring_delay(monkeypatch, tmp_path):
    calls = _run_notify_stop(
        monkeypatch, tmp_path,
        {"recurring": True, "recurring_delay": 60,
         "streak_reset_losses": 20, "streak_reset_delay": 900},
        "Profit target: +0.00100000")
    assert [d for d, _ in calls] == [60]


def test_tg_config_keys():
    from tg.config import CONFIG_KEYS
    assert "streak_reset_losses" in CONFIG_KEYS
    assert "streak_reset_delay" in CONFIG_KEYS
