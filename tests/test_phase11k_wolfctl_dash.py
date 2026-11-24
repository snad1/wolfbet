"""wolfctl dash: status, logs, start, stop and update for the dashboard on Coolify.

HOME is a temp dir holding the config install.sh writes; docker, systemctl, journalctl
and curl are stubs that log their arguments; git is real, against a temp origin and a
checkout standing in for ~/casino-dashboard. No container, unit or network is touched.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11k_wolfctl_dash.py -q
"""

import os
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFCTL = os.path.join(os.path.dirname(_HERE), "wolfctl")
UUID = "k8s0abc123def456"
TOKEN = "1|secret-token-value"


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def host(tmp_path):
    home = tmp_path / "home"
    stubs = tmp_path / "bin"
    log = '"$HOME/calls.log"'
    for name in ("docker", "systemctl", "journalctl", "curl"):
        _write(str(stubs / name),
               f'#!/bin/sh\necho "{name} $*" >> {log}\n'
               'case "$*" in *is-active*) exit "${STUB_AGENT_DOWN:-0}" ;; esac\n'
               '[ "$1" = "-fsS" ] && exit "${STUB_CURL_EXIT:-0}"\nexit 0\n', 0o755)
    origin = tmp_path / "origin.git"
    dash = home / "casino-dashboard"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write(str(seed / "agent/install.sh"),
           'echo "agent install uid=$CASINO_AGENT_API_UID" >> "$HOME/calls.log"\n')
    _write(str(seed / ".gitignore"), "deploy/secrets/\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(dash)], check=True)
    return {"home": str(home), "stubs": str(stubs), "tmp": str(tmp_path),
            "dash": str(dash), "seed": str(seed)}


STACK = "wolfbet-dashboard"


def _configure(host, project=UUID, token=True, url="https://coolify.example.com"):
    conf = os.path.join(host["home"], ".config/casino-dashboard")
    _write(os.path.join(conf, "dash.env"),
           f"DASH_DIR={host['dash']}\nDASH_PROJECT={project}\n"
           f"DASH_HOST=wolfbet.example.com\nCOOLIFY_URL={url}\n", 0o600)
    if token:
        _write(os.path.join(conf, "coolify.header"), f"Authorization: Bearer {TOKEN}\n", 0o600)


def _wolfctl(host, *args, **env):
    full = {**os.environ, "HOME": host["home"],
            "PATH": host["stubs"] + os.pathsep + os.environ["PATH"], **env}
    return subprocess.run(["bash", _WOLFCTL, "dash", *args], capture_output=True, text=True,
                          timeout=30, env=full, cwd=host["tmp"])


def _calls(host):
    try:
        with open(os.path.join(host["home"], "calls.log")) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def test_without_install_every_subcommand_says_how_to_install(host):
    for action in ("status", "logs", "start", "stop", "update"):
        result = _wolfctl(host, action)
        assert result.returncode == 2
        assert "Dashboard not installed: run casino-dashboard/deploy/install.sh" in result.stderr
    assert _calls(host) == []


def test_unknown_subcommand_shows_usage(host):
    _configure(host)
    result = _wolfctl(host, "restart-everything")
    assert result.returncode == 2 and "Usage: wolfctl dash" in result.stderr


def test_status_shows_agent_containers_and_health(host):
    _configure(host)
    result = _wolfctl(host)
    assert result.returncode == 0
    assert "Agent:  running" in result.stdout
    assert "Health: ok (https://wolfbet.example.com)" in result.stdout
    assert _calls(host) == [
        "systemctl --user is-active --quiet casino-agent",
        f"docker compose -p {STACK} ps",
        "curl -fsS --max-time 5 https://wolfbet.example.com/api/healthz",
    ]
    down = _wolfctl(host, "status", STUB_AGENT_DOWN="1", STUB_CURL_EXIT="7")
    assert "Agent:  stopped" in down.stdout and "Health: no answer" in down.stdout


def test_logs_follow_a_service_the_stack_or_the_agent(host):
    _configure(host)
    assert _wolfctl(host, "logs", "api").returncode == 0
    assert _wolfctl(host, "logs").returncode == 0
    assert _wolfctl(host, "logs", "agent").returncode == 0
    assert _calls(host) == [
        f"docker compose -p {STACK} logs -f --tail 200 api",
        f"docker compose -p {STACK} logs -f --tail 200",
        "journalctl --user -u casino-agent -f --no-pager",
    ]
    bad = _wolfctl(host, "logs", "wolfbet")
    assert bad.returncode == 2 and "Unknown service: wolfbet" in bad.stderr


def test_start_and_stop_touch_only_the_dashboard(host):
    _configure(host)
    assert _wolfctl(host, "stop").returncode == 0
    assert _wolfctl(host, "start").returncode == 0
    calls = _calls(host)
    assert calls == [
        f"docker compose -p {STACK} stop",
        "systemctl --user stop casino-agent",
        "systemctl --user start casino-agent",
        f"docker compose -p {STACK} start",
    ]
    assert not any(c.startswith("systemctl") and "wolfbet" in c for c in calls)


def test_stack_commands_use_the_compose_project_not_the_coolify_app(host):
    _configure(host, project="")
    assert _wolfctl(host, "stop").returncode == 0
    assert _calls(host)[0] == f"docker compose -p {STACK} stop"
    status = _wolfctl(host, "status")
    assert status.returncode == 0 and f"docker compose -p {STACK} ps" in _calls(host)


def test_a_compose_project_override_is_honoured(host):
    _configure(host)
    conf = os.path.join(host["home"], ".config/casino-dashboard/dash.env")
    with open(conf, "a") as f:
        f.write("DASH_COMPOSE_PROJECT=other-stack\n")
    assert _wolfctl(host, "stop").returncode == 0
    assert _calls(host)[0] == "docker compose -p other-stack stop"


def test_update_pulls_updates_the_agent_and_deploys(host):
    _configure(host)
    _write(os.path.join(host["seed"], "NEW"), "x\n")
    _git(host["seed"], "add", "NEW")
    _git(host["seed"], "commit", "-q", "-m", "second")
    _git(host["seed"], "push", "-q", "origin", "main")
    _write(os.path.join(host["dash"], "deploy/secrets/session_secret.txt"), "ignored")
    result = _wolfctl(host, "update")
    assert result.returncode == 0, result.stderr
    assert os.path.exists(os.path.join(host["dash"], "NEW"))
    header = os.path.join(host["home"], ".config/casino-dashboard/coolify.header")
    assert _calls(host) == [
        "agent install uid=10001",
        f"curl -fsS --max-time 30 -H @{header} "
        f"https://coolify.example.com/api/v1/deploy?uuid={UUID}&force=false",
    ]
    assert TOKEN not in result.stdout + result.stderr
    assert "Deploy queued" in result.stdout


def test_update_without_a_token_asks_for_a_coolify_redeploy(host):
    _configure(host, token=False)
    result = _wolfctl(host, "update")
    assert result.returncode == 0
    assert "Redeploy in Coolify" in result.stdout
    assert not any(c.startswith("curl") for c in _calls(host))


def test_update_refuses_a_dirty_checkout(host):
    _configure(host)
    _write(os.path.join(host["dash"], "agent/install.sh"), "echo changed\n")
    result = _wolfctl(host, "update")
    assert result.returncode == 4
    assert "local changes" in result.stderr and "agent/install.sh" in result.stderr
    assert _calls(host) == []


def test_a_refused_deploy_fails(host):
    _configure(host)
    result = _wolfctl(host, "update", STUB_CURL_EXIT="22")
    assert result.returncode == 1
    assert "Coolify did not accept the deploy" in result.stderr


def test_version_and_usage_mention_dash():
    with open(_WOLFCTL) as f:
        text = f.read()
    assert "WOLFCTL_VERSION=3.26" in text
    assert "wolfctl dash status" in text
