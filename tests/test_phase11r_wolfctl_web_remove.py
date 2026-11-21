"""wolfctl web: the old web is gone; `web remove` clears a leftover unit and keeps its data.

HOME is a temp dir; systemctl is a stub that logs its arguments. No unit is touched.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11r_wolfctl_web_remove.py -q
"""

import os
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFCTL = os.path.join(os.path.dirname(_HERE), "wolfctl")
URL_LINE = ("The old web was removed: use the dashboard at https://wolfbet.example.com "
            "(Telegram users: send /web to the bot).")
UNIT = ".config/systemd/user/wolfbet-web.service"


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


@pytest.fixture
def host(tmp_path):
    home = tmp_path / "home"
    stubs = tmp_path / "bin"
    _write(str(stubs / "systemctl"),
           '#!/bin/sh\necho "systemctl $*" >> "$HOME/calls.log"\n'
           'case "$*" in *is-active*) exit 3 ;; esac\nexit 0\n', 0o755)
    return {"home": str(home), "stubs": str(stubs), "tmp": str(tmp_path)}


def _leftover(host):
    _write(os.path.join(host["home"], UNIT), "[Service]\nExecStart=/bin/true\n")
    _write(os.path.join(host["home"], "wolfbet-web/web.db"), "data")


def _dashboard(host):
    _write(os.path.join(host["home"], ".config/casino-dashboard/dash.env"),
           "DASH_DIR=/x\nDASH_PROJECT=p\nDASH_HOST=wolfbet.example.com\nCOOLIFY_URL=\n", 0o600)


def _wolfctl(host, *args):
    env = {**os.environ, "HOME": host["home"],
           "PATH": host["stubs"] + os.pathsep + os.environ["PATH"]}
    return subprocess.run(["bash", _WOLFCTL, *args], capture_output=True, text=True,
                          timeout=30, env=env, cwd=host["tmp"])


def _calls(host):
    try:
        with open(os.path.join(host["home"], "calls.log")) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def test_remove_deletes_the_leftover_unit_and_keeps_its_data(host):
    _leftover(host)
    _dashboard(host)
    result = _wolfctl(host, "web", "remove")
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == URL_LINE
    assert "Old web unit stopped, disabled and deleted." in lines
    assert any("delete it by hand" in line and "wolfbet-web" in line for line in lines)
    assert _calls(host) == [
        "systemctl --user stop wolfbet-web",
        "systemctl --user disable wolfbet-web",
        "systemctl --user daemon-reload",
    ]
    assert not os.path.exists(os.path.join(host["home"], UNIT))
    assert os.path.exists(os.path.join(host["home"], "wolfbet-web/web.db"))


def test_remove_with_nothing_installed_changes_nothing(host):
    result = _wolfctl(host, "web", "remove")
    assert result.returncode == 0
    assert "The old web is not installed; nothing to remove." in result.stdout
    assert _calls(host) == []


@pytest.mark.parametrize("args", [("web",), ("web", "start"), ("web", "status"), ("web", "install")])
def test_every_other_web_command_only_points_to_the_dashboard(host, args):
    _leftover(host)
    _dashboard(host)
    result = _wolfctl(host, *args)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [URL_LINE]
    assert _calls(host) == []


def test_without_the_dashboard_the_pointer_says_how_to_install_it(host):
    result = _wolfctl(host, "web")
    assert result.stdout.splitlines() == [
        "The old web was removed: install the dashboard (casino-dashboard/deploy/install.sh)."]


def test_status_all_shows_a_leftover_unit_only_while_it_exists(host):
    assert "Old web" not in _wolfctl(host, "status-all").stdout
    _leftover(host)
    assert "Old web:      leftover unit (wolfctl web remove)" in _wolfctl(host, "status-all").stdout


def test_stop_all_still_stops_a_leftover_web(host):
    _leftover(host)
    _wolfctl(host, "stop-all")
    assert "systemctl --user stop wolfbet-web" in _calls(host)


def test_update_has_no_old_web_step_left():
    with open(_WOLFCTL) as f:
        source = f.read()
    assert "web/install.sh" not in source
    assert 'cp -r web/' not in source
    assert "wolfbet-web.service; do" not in source
