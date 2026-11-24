"""A lane restarts or is removed at its session end, when asked, and never mid-session."""

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


@pytest.fixture
def lanes(monkeypatch, tmp_path, capsys):
    """Lanes 1-3 in the registry, drain files in tmp, this process acting as lane 2."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"lanes": {"count": 3}}))
    monkeypatch.setattr(wb, "CONFIG_PATH", str(config))
    monkeypatch.setattr(wb, "DRAIN_PATH", str(tmp_path / "drain"))
    monkeypatch.setattr(wb, "state", wb.BotState())
    wb.state.lane = 2
    calls = {"exec": [], "popen": [], "flushed": []}
    monkeypatch.setattr(wb.os, "execv", lambda exe, argv: calls["exec"].append(argv))
    monkeypatch.setattr(wb, "_stop_bet_writer", lambda timeout=5.0: calls["flushed"].append("writer"))
    monkeypatch.setattr(wb, "_save_state_file", lambda: calls["flushed"].append("state"))
    monkeypatch.setattr(wb, "_stop_events", lambda: calls["flushed"].append("events"))
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls["popen"].append(argv))
    return calls


def _flag(lane):
    return wb._lane_path(wb.DRAIN_PATH, lane)


def test_a_request_is_written_and_cancelled(lanes, capsys):
    wb.cmd_lane_drain(2, "restart")
    assert open(_flag(2)).read() == "restart"
    assert "Lane 2 restarts when its session ends. It keeps betting until then." in capsys.readouterr().out
    assert oct(os.stat(_flag(2)).st_mode & 0o777) == "0o600"
    wb.cmd_lane_drain(2, "cancel")
    assert not os.path.exists(_flag(2))
    assert "Lane 2 no longer waits for its session end." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("lane", "action", "message"),
    [(9, "restart", "No lane 9."), (1, "remove", "Lane 1 holds the base config and can't be removed.")],
)
def test_bad_requests_write_nothing(lanes, capsys, lane, action, message):
    with pytest.raises(SystemExit) as exc:
        wb.cmd_lane_drain(lane, action)
    assert exc.value.code == 2 and message in capsys.readouterr().out
    assert not os.path.exists(_flag(lane))


def test_no_request_means_the_session_carries_on(lanes):
    assert wb._session_boundary(final=False) is False
    assert lanes == {"exec": [], "popen": [], "flushed": []}


def test_a_restart_flushes_then_re_executes_the_same_command(lanes):
    wb.cmd_lane_drain(2, "restart")
    wb._session_boundary(final=False)
    assert lanes["flushed"] == ["writer", "state", "events"]
    assert lanes["exec"] == [[sys.executable] + sys.argv]
    assert not os.path.exists(_flag(2))


def test_with_recurring_off_a_restart_is_dropped_and_the_lane_stops(lanes):
    wb.cmd_lane_drain(2, "restart")
    assert wb._session_boundary(final=True) is False
    assert lanes["exec"] == [] and not os.path.exists(_flag(2))


def test_a_removal_hands_the_lane_to_wolfctl_and_stops_it(lanes):
    for final in (False, True):
        wb.cmd_lane_drain(2, "remove")
        assert wb._session_boundary(final=final) is True
    assert lanes["popen"] == [[wb.WOLFCTL, "lane", "remove", "2"]] * 2
    assert lanes["exec"] == [] and not os.path.exists(_flag(2))


def test_another_lanes_request_is_left_alone(lanes):
    wb.cmd_lane_drain(3, "restart")
    assert wb._session_boundary(final=False) is False
    assert os.path.exists(_flag(3)) and lanes["exec"] == []


from tests.test_phase11c_bet_events import BETS, bot  # noqa: E402,F401  # the scripted betting loop


class _Reexec(Exception):
    """Stands in for os.execv, which would replace the test process."""


def _drain(tmp_path, monkeypatch, action):
    monkeypatch.setattr(wb, "DRAIN_PATH", str(tmp_path / "drain"))
    with open(wb._lane_path(wb.DRAIN_PATH, 1), "w") as f:
        f.write(action)


def test_the_loop_restarts_at_the_session_end_and_not_before(bot, tmp_path, monkeypatch):  # noqa: F811
    bot.state.recurring = True
    _drain(tmp_path, monkeypatch, "restart")

    def reexec(exe, argv):
        raise _Reexec

    monkeypatch.setattr(wb.os, "execv", reexec)
    with pytest.raises(_Reexec):
        bot.betting_loop()
    assert bot.state.total_bets == BETS


def test_the_loop_stops_for_a_removal_instead_of_starting_a_new_session(
    bot, tmp_path, monkeypatch  # noqa: F811
):
    import subprocess

    bot.state.recurring = True
    _drain(tmp_path, monkeypatch, "remove")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    first = bot.state.session_id
    bot.betting_loop()
    assert bot.state.total_bets == BETS and bot.state.running is False
    assert bot.state.session_id == first
    assert started == [[wb.WOLFCTL, "lane", "remove", "1"]]


def test_with_recurring_off_the_loop_stops_as_before(bot, tmp_path, monkeypatch):  # noqa: F811
    _drain(tmp_path, monkeypatch, "restart")
    monkeypatch.setattr(wb.os, "execv", lambda exe, argv: pytest.fail("must not restart"))
    bot.betting_loop()
    assert bot.state.total_bets == BETS and bot.state.running is False


def test_each_lane_says_which_code_it_runs(lanes):
    assert wb._state_snapshot()["code_version"] == wb.VERSION
    assert wb._lane_code(2, {"code_version": wb.VERSION}) == "new"
    assert wb._lane_code(2, {"code_version": "2.0.0"}) == "old"
    wb.cmd_lane_drain(2, "restart")
    assert wb._lane_code(2, {"code_version": "2.0.0"}) == "waiting for session end"
    assert wb._lane_code(2, {"code_version": wb.VERSION}) == "new"
    assert wb._lane_code(3, None) == "old"
