"""wolfctl pause / resume / reload, --yes, no-terminal refusal and --json passthrough.

Each lane daemon is stood in for by a real child process that records the
signals it gets. /proc is faked through WOLFCTL_PROC_DIR, and HOME holds a stub
python3 that logs its arguments, so no real daemon, DB or systemd is touched.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11i_wolfctl_verbs.py -q
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFCTL = os.path.join(os.path.dirname(_HERE), "wolfctl")

_RECORDER = textwrap.dedent("""
    import signal, sys, time
    out = sys.argv[1]
    def note(sig, frame):
        with open(out, "a") as f:
            f.write(signal.Signals(sig).name + "\\n")
    for s in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGHUP):
        signal.signal(s, note)
    open(out + ".ready", "w").close()
    while True:
        time.sleep(0.02)
""")


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


# Like systemd: `is-active` prints the state unless --quiet, and exits 3 for a stopped unit.
# Units named in $HOME/active.txt stay active; $HOME/no-bus makes every call fail like a
# missing XDG_RUNTIME_DIR does.
_SYSTEMCTL = textwrap.dedent("""\
    #!/bin/sh
    [ -f "$HOME/no-bus" ] && { echo "Failed to connect to bus: No medium found" >&2; exit 1; }
    case "$*" in
        *is-active*)
            unit=$(for a in "$@"; do last=$a; done; echo "$last")
            if grep -qx "$unit" "$HOME/active.txt" 2>/dev/null; then state=active; code=0
            else state=inactive; code=3; fi
            case "$*" in *--quiet*) ;; *) echo "$state" ;; esac
            exit $code ;;
        *is-enabled*) exit 1 ;;
    esac
    exit 0
    """)


@pytest.fixture
def host(tmp_path):
    """Temp HOME with a stub python3, stub systemctl/sqlite3/free/pgrep and an empty fake /proc."""
    home = tmp_path / "home"
    stubs = tmp_path / "bin"
    _write(str(home / "wolfbet-bot/venv/bin/python3"), textwrap.dedent("""\
        #!/bin/sh
        echo "$*" >> "$HOME/python.log"
        [ "$1" = - ] && [ -f "$HOME/lanes.txt" ] && { cat "$HOME/lanes.txt"; exit 0; }
        case "$*" in *--json*) echo '{"v": 1}' ;; esac
        """), 0o755)
    _write(str(stubs / "systemctl"), _SYSTEMCTL, 0o755)
    _write(str(stubs / "sqlite3"), "#!/bin/sh\necho 0\n", 0o755)
    _write(str(stubs / "journalctl"), "#!/bin/sh\nexit 0\n", 0o755)
    _write(str(stubs / "free"), "#!/bin/sh\necho mem\n", 0o755)
    _write(str(stubs / "pgrep"), "#!/bin/sh\nexit 0\n", 0o755)
    os.makedirs(tmp_path / "proc")
    children = []
    yield {"home": str(home), "stubs": str(stubs), "proc": str(tmp_path / "proc"),
           "tmp": str(tmp_path), "children": children}
    for child in children:
        child.kill()
        child.wait()


def _wolfctl(host, *args, stdin=subprocess.DEVNULL, cwd=None):
    env = {**os.environ, "HOME": host["home"], "WOLFCTL_PROC_DIR": host["proc"],
           "PATH": host["stubs"] + os.pathsep + os.environ["PATH"]}
    return subprocess.run(["bash", _WOLFCTL, *args], capture_output=True, text=True,
                          timeout=20, env=env, stdin=stdin, cwd=cwd or host["tmp"])


def _python_calls(host) -> list:
    try:
        with open(os.path.join(host["home"], "python.log")) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _lane(host, k: int, cmdline: str = "python3\0/root/wolfbet-bot/wolfbet.py\0--daemon\0"):
    """Start a recorder for lane k, write its pid file and its fake /proc cmdline."""
    out = os.path.join(host["tmp"], f"signals{k}.txt")
    child = subprocess.Popen([sys.executable, "-c", _RECORDER, out])
    host["children"].append(child)
    for _ in range(200):
        if os.path.exists(out + ".ready"):
            break
        time.sleep(0.01)
    name = ".wolfbet_autobot.pid" if k == 1 else f".wolfbet_autobot.lane{k}.pid"
    _write(os.path.join(host["home"], name), f"{child.pid}\n")
    _write(os.path.join(host["proc"], str(child.pid), "cmdline"), cmdline)
    return child.pid, out


def _received(path: str, expect: int = 1) -> list:
    for _ in range(200):
        if os.path.exists(path):
            with open(path) as f:
                got = f.read().split()
            if len(got) >= expect:
                return got
        time.sleep(0.01)
    return []


# ── pause / resume / reload ──
@pytest.mark.parametrize("verb,signame", [("pause", "SIGUSR1"), ("resume", "SIGUSR2"),
                                          ("reload", "SIGHUP")])
def test_verb_sends_its_signal_to_the_named_lane_only(host, verb, signame):
    pid1, out1 = _lane(host, 1)
    pid2, out2 = _lane(host, 2)
    r = _wolfctl(host, verb, "2")
    assert r.returncode == 0, r.stderr
    assert f"lane 2: {verb} sent (pid {pid2})" in r.stdout
    assert _received(out2) == [signame]
    time.sleep(0.1)
    assert not os.path.exists(out1)


def test_default_and_all_reach_every_running_lane(host):
    _, out1 = _lane(host, 1)
    _, out3 = _lane(host, 3)
    _write(os.path.join(host["home"], ".wolfbet_autobot.lane2.pid"), "999999\n")   # stale
    r = _wolfctl(host, "pause")
    assert r.returncode == 0, r.stderr
    assert "lane 2: not running (stale pid 999999)" in r.stderr
    r = _wolfctl(host, "resume", "all")
    assert r.returncode == 0
    assert _received(out1, 2) == ["SIGUSR1", "SIGUSR2"]
    assert _received(out3, 2) == ["SIGUSR1", "SIGUSR2"]


def test_pid_that_is_not_wolfbet_is_refused(host):
    pid, out = _lane(host, 1, cmdline="/usr/sbin/sshd\0-D\0")
    r = _wolfctl(host, "pause", "1")
    assert r.returncode == 1
    assert f"lane 1: pid {pid} is not wolfbet.py, refused" in r.stderr
    assert "No running lane matched." in r.stderr
    time.sleep(0.1)
    assert not os.path.exists(out)


@pytest.mark.parametrize("target", ["0", "11", "x", "1;id", "-1"])
def test_bad_lane_number_exits_2(host, target):
    r = _wolfctl(host, "reload", target)
    assert r.returncode == 2 and "Usage: wolfctl reload [K|all]" in r.stderr


def test_no_pid_files_exits_1(host):
    r = _wolfctl(host, "reload")
    assert r.returncode == 1 and "No running lane matched." in r.stderr


def test_garbage_pid_file_is_skipped(host):
    _write(os.path.join(host["home"], ".wolfbet_autobot.pid"), "12; rm -rf /\n")
    r = _wolfctl(host, "pause", "1")
    assert r.returncode == 1 and "lane 1: pid file holds no pid, skipped" in r.stderr


# ── prompts: --yes, or exit 3 without a terminal ──
def _db(host):
    path = os.path.join(host["home"], ".wolfbet_autobot.db")
    _write(path, "db")
    return path


def test_reset_without_terminal_refuses_with_exit_3(host):
    db = _db(host)
    r = _wolfctl(host, "reset")
    assert r.returncode == 3
    assert "needs confirmation and there is no terminal; re-run with --yes" in r.stderr
    assert os.path.exists(db)


def test_reset_with_an_open_pipe_does_not_hang(host):
    db = _db(host)
    env = {**os.environ, "HOME": host["home"], "PATH": host["stubs"] + os.pathsep + os.environ["PATH"]}
    proc = subprocess.Popen(["bash", _WOLFCTL, "reset"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=host["tmp"])
    try:
        assert proc.wait(timeout=10) == 3
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
    assert os.path.exists(db)


def test_reset_yes_archives_without_asking(host):
    db = _db(host)
    r = _wolfctl(host, "reset", "--yes")
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(db)
    assert any(n.startswith(".wolfbet_autobot-") for n in os.listdir(host["home"]))


def test_run_reset_without_terminal_refuses_with_exit_3(host):
    r = _wolfctl(host, "run-reset")
    assert r.returncode == 3 and "--run-reset" not in " ".join(_python_calls(host))
    r = _wolfctl(host, "run-reset", "-y")
    assert r.returncode == 0, r.stderr
    assert _python_calls(host)[-1] == "/root/wolfbet-bot/wolfbet.py --run-reset".replace(
        "/root", host["home"])


def test_doctor_fix_uses_the_same_confirmation():
    src = open(_WOLFCTL).read()
    fix = src[src.index("_doctor_run_fix() {"):src.index("_doctor_run_fix \"Force")]
    assert 'confirm "  $desc - apply?"' in fix and "read -r" not in fix
    assert src.count("read -r -p") == 1, "only confirm() may read from the terminal"


# ── --yes on commands that never prompt ──
def test_lanes_accepts_yes(host):
    r = _wolfctl(host, "lanes", "2", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("--config-set lanes=2" in c for c in _python_calls(host))


def test_cleanup_accepts_yes_and_keeps_days(host):
    r = _wolfctl(host, "cleanup", "--yes", "7")
    assert r.returncode == 0, r.stderr
    assert _python_calls(host)[-1].endswith("--cleanup 7")
    _wolfctl(host, "cleanup", "-y")
    assert _python_calls(host)[-1].endswith("--cleanup")


def test_stop_all_fails_and_names_a_unit_that_is_still_running(host):
    _write(os.path.join(host["home"], "active.txt"), "wolfbet-lane@3\n")
    r = _wolfctl(host, "stop-all")
    assert r.returncode == 1
    assert "Still running: wolfbet-lane@3 (active)" in r.stderr
    assert "All services stopped." not in r.stdout


def test_stop_all_fails_when_the_user_systemd_cannot_be_reached(host):
    _write(os.path.join(host["home"], "no-bus"), "")
    r = _wolfctl(host, "stop-all")
    assert r.returncode == 1
    assert "Cannot reach the user's systemd" in r.stderr
    assert "All services stopped." not in r.stdout


def test_stop_all_and_update_accept_yes(host):
    r = _wolfctl(host, "stop-all", "--yes")
    assert r.returncode == 0 and "All services stopped." in r.stdout
    plain = _wolfctl(host, "update")
    yes = _wolfctl(host, "update", "--yes")
    assert yes.returncode == plain.returncode and yes.stdout == plain.stdout


# ── --json passthrough ──
def test_status_json_prints_only_the_document(host):
    r = _wolfctl(host, "status", "--json")
    assert r.returncode == 0 and r.stdout == '{"v": 1}\n'
    assert _python_calls(host)[-1].endswith("wolfbet.py --status --json")


def test_lanes_json_prints_only_the_document(host):
    r = _wolfctl(host, "lanes", "--json")
    assert r.returncode == 0 and r.stdout == '{"v": 1}\n'
    assert _python_calls(host)[-1].endswith("wolfbet.py --lanes-status --json")
    r = _wolfctl(host, "lanes", "2", "--json")
    assert r.returncode == 2


def test_lanes_split_without_count_is_rejected(host):
    r = _wolfctl(host, "lanes", "--split", "bet")
    assert r.returncode == 1 and "Usage: wolfctl lanes N --split" in r.stdout


# ── surface ──
def test_usage_lists_verbs_and_exit_codes(host):
    out = _wolfctl(host, "help").stdout
    for verb in ("wolfctl pause [K|all]", "wolfctl resume [K|all]", "wolfctl reload [K|all]"):
        assert verb in out
    assert "3 needs confirmation and there is no terminal" in out
    assert "wolfctl v3.22" in _wolfctl(host, "version").stdout


def test_wolfctl_syntax_valid():
    assert subprocess.run(["bash", "-n", _WOLFCTL]).returncode == 0


def test_stop_one_lane_leaves_the_others(host):
    _write(os.path.join(host["home"], "lanes.txt"), "3\n")
    r = _wolfctl(host, "stop", "2")
    assert r.returncode == 0, r.stderr
    assert "Lane 2 stopped. Other lanes keep betting." in r.stdout


def test_a_lane_that_keeps_running_fails_the_stop(host):
    _write(os.path.join(host["home"], "lanes.txt"), "3\n")
    _write(os.path.join(host["home"], "active.txt"), "wolfbet-lane@3\n")
    r = _wolfctl(host, "stop", "3")
    assert r.returncode == 1 and "Lane 3 is still active." in r.stderr


def test_start_one_lane_checks_it_came_up(host):
    _write(os.path.join(host["home"], "lanes.txt"), "3\n")
    _write(os.path.join(host["home"], "active.txt"), "wolfbet-lane@2\n")
    assert "Lane 2 started." in _wolfctl(host, "start", "2").stdout
    r = _wolfctl(host, "start", "3")
    assert r.returncode == 1 and "Lane 3 did not start (inactive)." in r.stderr


@pytest.mark.parametrize("lane,message", [
    ("1", "Lane 1 is the main service"),
    ("4", "No lane 4: this bot runs 3 lane(s)."),
    ("x", "Usage: wolfctl stop [K]"),
])
def test_a_lane_stop_refuses_lane_one_and_lanes_that_do_not_exist(host, lane, message):
    _write(os.path.join(host["home"], "lanes.txt"), "3\n")
    r = _wolfctl(host, "stop", lane)
    assert r.returncode == 2 and message in r.stderr
