"""wolfctl update rolls through the lanes at their session ends; --critical restarts them now."""

import os

import pytest

from tests.test_phase11i_wolfctl_verbs import (  # noqa: F401  # host is a fixture
    _lanes,
    _python_calls,
    _systemctl_calls,
    _wolfctl,
    _write,
    host,
)


@pytest.fixture
def repo(host):
    """A checkout to update from, a stub pip, and lanes 1-3 with lanes 1 and 3 running."""
    checkout = os.path.join(host["tmp"], "checkout")
    _write(os.path.join(checkout, "wolfbet.py"), "VERSION = 'new'\n")
    _write(os.path.join(checkout, "requirements.txt"), "")
    _write(os.path.join(host["home"], "wolfbet-bot/venv/bin/pip"), "#!/bin/sh\nexit 0\n", 0o755)
    _lanes(host, "1 2 3")
    _write(os.path.join(host["home"], "active.txt"), "wolfbet\nwolfbet-lane@3\n")
    return checkout


def test_an_update_asks_each_running_lane_to_restart_at_its_session_end(host, repo):
    r = _wolfctl(host, "update", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "Lane 1 restarts on the new code when its session ends." in r.stdout
    assert "Lane 3 restarts on the new code when its session ends." in r.stdout
    assert "Lane 2 restarts" not in r.stdout
    assert "Betting goes on; use wolfctl update --critical to restart every lane now." in r.stdout
    drains = [c for c in _python_calls(host) if "--lane-drain" in c]
    assert [c.split("wolfbet.py ")[1] for c in drains] == ["--lane-drain 1 restart",
                                                          "--lane-drain 3 restart"]
    assert not any(c.startswith(("--user stop", "--user restart")) for c in _systemctl_calls(host))


def test_a_critical_update_restarts_every_lane_now(host, repo):
    r = _wolfctl(host, "update", "--critical", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "--user restart wolfbet" in _systemctl_calls(host)
    assert not any("--lane-drain" in c for c in _python_calls(host))
    assert "Betting goes on" not in r.stdout


def test_nothing_is_asked_of_lanes_when_the_bot_is_stopped(host, repo):
    _write(os.path.join(host["home"], "active.txt"), "")
    r = _wolfctl(host, "update", cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "CLI bot: wolfctl restart" in r.stdout
    assert not any("--lane-drain" in c for c in _python_calls(host))


def test_an_unknown_update_option_exits_2(host, repo):
    r = _wolfctl(host, "update", "--soon", cwd=repo)
    assert r.returncode == 2
    assert "Usage: wolfctl update [--branch NAME] [--critical] [--yes]" in r.stderr
