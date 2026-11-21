"""wolfctl web retire / restore: the old web gives way to the dashboard, one command each way.

HOME is a temp dir; systemctl is a stub that logs its arguments and answers `is-active` and
`is-enabled` from STUB_WEB_ACTIVE / STUB_WEB_ENABLED. No unit is touched.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11r_wolfctl_web_retire.py -q
"""

import os
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFCTL = os.path.join(os.path.dirname(_HERE), "wolfctl")
URL_LINE = ("The old web is retired: use the dashboard at https://wolfbet.example.com "
            "(Telegram users: send /web to the bot).")


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
           'case "$*" in\n'
           '  *is-active*wolfbet-web*) exit "${STUB_WEB_ACTIVE:-3}" ;;\n'
           '  *is-active*) exit 3 ;;\n'
           '  *is-enabled*wolfbet-web*) echo "${STUB_WEB_ENABLED:-enabled}" ;;\n'
           'esac\nexit 0\n', 0o755)
    _write(str(stubs / "sleep"), "#!/bin/sh\nexit 0\n", 0o755)
    return {"home": str(home), "stubs": str(stubs), "tmp": str(tmp_path)}


def _install_web(host):
    _write(os.path.join(host["home"], ".config/systemd/user/wolfbet-web.service"),
           "[Service]\nExecStart=/bin/true\n")


def _dashboard(host):
    _write(os.path.join(host["home"], ".config/casino-dashboard/dash.env"),
           "DASH_DIR=/x\nDASH_PROJECT=p\nDASH_HOST=wolfbet.example.com\nCOOLIFY_URL=\n", 0o600)


def _wolfctl(host, *args, **env):
    full = {**os.environ, "HOME": host["home"],
            "PATH": host["stubs"] + os.pathsep + os.environ["PATH"], **env}
    return subprocess.run(["bash", _WOLFCTL, *args], capture_output=True, text=True,
                          timeout=30, env=full, cwd=host["tmp"])


def _calls(host):
    try:
        with open(os.path.join(host["home"], "calls.log")) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def test_retire_stops_and_disables_and_keeps_the_files(host):
    _install_web(host)
    _dashboard(host)
    result = _wolfctl(host, "web", "retire")
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == URL_LINE
    assert "Old web stopped and disabled; its files stay for rollback." in result.stdout
    assert "Bring it back with: wolfctl web restore" in result.stdout
    assert _calls(host) == [
        "systemctl --user stop wolfbet-web",
        "systemctl --user disable wolfbet-web",
    ]
    assert os.path.exists(os.path.join(host["home"], ".config/systemd/user/wolfbet-web.service"))


def test_restore_enables_and_starts_it_again(host):
    _install_web(host)
    result = _wolfctl(host, "web", "restore", STUB_WEB_ACTIVE="0")
    assert result.returncode == 0
    assert "Old web restored and running." in result.stdout
    assert _calls(host) == [
        "systemctl --user enable --now wolfbet-web",
        "systemctl --user is-active --quiet wolfbet-web",
    ]


def test_restore_that_fails_to_start_says_where_to_look(host):
    _install_web(host)
    result = _wolfctl(host, "web", "restore", STUB_WEB_ACTIVE="3")
    assert result.returncode == 1
    assert "Failed to start. Check: wolfctl web logs" in result.stdout


def test_without_the_web_installed_retire_is_a_no_op(host):
    result = _wolfctl(host, "web", "retire")
    assert result.returncode == 0
    assert "The old web is not installed; nothing to retire." in result.stdout
    assert _calls(host) == []


def test_every_web_command_names_the_dashboard_or_how_to_get_it(host):
    _install_web(host)
    without = _wolfctl(host, "web", "status")
    assert without.stdout.splitlines()[0] == (
        "The old web is retired: install the dashboard (casino-dashboard/deploy/install.sh).")
    _dashboard(host)
    for args in (("web",), ("web", "status"), ("web", "stop"), ("web", "nonsense")):
        assert _wolfctl(host, *args).stdout.splitlines()[0] == URL_LINE, args
    assert "retire|restore|" in _wolfctl(host, "web", "nonsense").stdout


def test_status_all_shows_a_retired_web(host):
    _install_web(host)
    result = _wolfctl(host, "status-all", STUB_WEB_ENABLED="disabled")
    assert "Web Admin:    ○ retired (wolfctl web restore to bring it back)" in result.stdout
    assert "Web Admin:    ○ stopped" in _wolfctl(host, "status-all").stdout
    running = _wolfctl(host, "status-all", STUB_WEB_ACTIVE="0").stdout
    assert "Web Admin:    ● RUNNING (retired: wolfctl web retire)" in running


def test_stop_all_stops_the_web_and_start_all_never_starts_it(host):
    _install_web(host)
    _wolfctl(host, "stop-all")
    assert "systemctl --user stop wolfbet-web" in _calls(host)
    os.remove(os.path.join(host["home"], "calls.log"))
    _wolfctl(host, "start-all")
    started = [c for c in _calls(host) if "wolfbet-web" in c and " start" in c]
    assert started == []
