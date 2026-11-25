"""wolfctl update --branch and the Code line in wolfctl status.

Runs the real wolfctl and real git against temp repos: a bare "origin" and a
clone that plays the server checkout. HOME is a temp dir with stub pip and
python3 and a stub systemctl, so nothing on the host is touched.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11o_wolfctl_branch.py -q
"""

import os
import subprocess
import textwrap

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFCTL = os.path.join(os.path.dirname(_HERE), "wolfctl")
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1"}


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


@pytest.fixture
def site(tmp_path):
    """origin.git with main and feat/x, a checkout cloned on main, a temp HOME with stubs."""
    home, stubs = tmp_path / "home", tmp_path / "bin"
    env = {**os.environ, **_GIT_ENV, "HOME": str(home),
           "PATH": str(stubs) + os.pathsep + os.environ["PATH"]}

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    _write(str(home / "wolfbet-bot/venv/bin/pip"), "#!/bin/sh\nexit 0\n", 0o755)
    _write(str(home / "wolfbet-bot/venv/bin/python3"), "#!/bin/sh\nexit 0\n", 0o755)
    _write(str(stubs / "systemctl"), '#!/bin/sh\ncase "$*" in *is-active*|*is-enabled*) exit 1 ;; esac\nexit 0\n', 0o755)
    origin, seed, checkout = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "checkout"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    _write(str(seed / "wolfbet.py"), "VERSION = 'main'\n")
    _write(str(seed / "requirements.txt"), "rich\n")
    _write(str(seed / "README.md"), "readme\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "main")
    git(seed, "push", "origin", "main")
    git(seed, "switch", "-c", "feat/x")
    _write(str(seed / "wolfbet.py"), "VERSION = 'feat'\n")
    git(seed, "commit", "-am", "feat")
    git(seed, "push", "origin", "feat/x")
    git(seed, "switch", "main")
    git(tmp_path, "clone", str(origin), str(checkout))
    return {"env": env, "git": git, "home": str(home), "seed": str(seed),
            "checkout": str(checkout), "tmp": str(tmp_path)}


def _wolfctl(site, *args, cwd=None):
    return subprocess.run(["bash", _WOLFCTL, *args], cwd=cwd or site["checkout"], env=site["env"],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def _installed(site) -> str:
    path = os.path.join(site["home"], "wolfbet-bot", "wolfbet.py")
    return open(path).read() if os.path.exists(path) else ""


def _branch(site) -> str:
    return site["git"](site["checkout"], "rev-parse", "--abbrev-ref", "HEAD")


def test_switch_to_a_branch_only_on_origin_creates_it_with_tracking(site):
    r = _wolfctl(site, "update", "--branch", "feat/x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _branch(site) == "feat/x"
    assert site["git"](site["checkout"], "rev-parse", "--abbrev-ref", "feat/x@{upstream}") == "origin/feat/x"
    assert _installed(site) == "VERSION = 'feat'\n"
    with open(os.path.join(site["home"], "wolfbet-bot", ".repo")) as f:
        assert os.path.realpath(f.read().strip()) == os.path.realpath(site["checkout"])


def test_switch_back_to_an_existing_local_branch(site):
    assert _wolfctl(site, "update", "--branch=feat/x").returncode == 0
    r = _wolfctl(site, "update", "--branch", "main", "--yes")
    assert r.returncode == 0, r.stderr
    assert _branch(site) == "main" and _installed(site) == "VERSION = 'main'\n"


def test_branch_picks_up_new_commits_on_origin(site):
    assert _wolfctl(site, "update", "--branch", "feat/x").returncode == 0
    site["git"](site["seed"], "switch", "feat/x")
    _write(os.path.join(site["seed"], "wolfbet.py"), "VERSION = 'feat2'\n")
    site["git"](site["seed"], "commit", "-am", "feat2")
    site["git"](site["seed"], "push", "origin", "feat/x")
    assert _wolfctl(site, "update", "--branch", "feat/x").returncode == 0
    assert _installed(site) == "VERSION = 'feat2'\n"


@pytest.mark.parametrize("change", [
    lambda c: open(os.path.join(c, "README.md"), "a").write("local edit\n"),
    lambda c: open(os.path.join(c, "notes.txt"), "w").write("untracked\n"),
])
def test_local_changes_refuse_with_exit_4_and_nothing_moves(site, change):
    change(site["checkout"])
    r = _wolfctl(site, "update", "--branch", "feat/x")
    assert r.returncode == 4
    assert "local changes; commit or stash them first" in r.stderr
    assert _branch(site) == "main" and _installed(site) == ""


@pytest.mark.parametrize("name", ["a..b", "-x", "bad name", "x~1", "has:colon", "--force", ""])
def test_invalid_branch_names_exit_2(site, name):
    r = _wolfctl(site, "update", "--branch", name)
    assert r.returncode == 2 and "Invalid branch name" in r.stderr
    assert _branch(site) == "main" and _installed(site) == ""


def test_unknown_branch_exits_2(site):
    r = _wolfctl(site, "update", "--branch", "nope")
    assert r.returncode == 2 and "No branch 'nope' locally or on origin." in r.stderr
    assert _branch(site) == "main" and _installed(site) == ""


def test_rewritten_branch_is_never_reset(site):
    assert _wolfctl(site, "update", "--branch", "feat/x").returncode == 0
    before = site["git"](site["checkout"], "rev-parse", "HEAD")
    site["git"](site["seed"], "switch", "feat/x")
    site["git"](site["seed"], "commit", "--amend", "-m", "rewritten")
    site["git"](site["seed"], "push", "--force", "origin", "feat/x")
    r = _wolfctl(site, "update", "--branch", "feat/x")
    assert r.returncode == 1 and "the branch was probably rewritten" in r.stderr
    assert site["git"](site["checkout"], "rev-parse", "HEAD") == before


def test_outside_a_checkout_exits_2(site):
    r = _wolfctl(site, "update", "--branch", "main", cwd=site["tmp"])
    assert r.returncode == 2 and "must run inside the repo checkout" in r.stderr


def test_unknown_option_exits_2(site):
    r = _wolfctl(site, "update", "--bogus")
    assert r.returncode == 2 and "Unknown option: --bogus" in r.stderr


def test_plain_update_keeps_the_autostash_pull(site):
    src = open(_WOLFCTL).read()
    plain = src[src.index("cmd_update() {"):src.index('echo "Updating bot files..."')]
    assert "git pull --ff-only --autostash" in plain
    assert "reset" not in src[src.index("switch_branch() {"):src.index("repo_code_line() {")]
    r = _wolfctl(site, "update")
    assert r.returncode == 0, r.stderr
    assert _branch(site) == "main" and _installed(site) == "VERSION = 'main'\n"


def test_status_shows_branch_commit_and_lead_over_main(site):
    r = _wolfctl(site, "status", cwd=site["tmp"])
    assert "Code: unknown (run wolfctl update once from the repo checkout)" in r.stdout
    assert _wolfctl(site, "update", "--branch", "feat/x").returncode == 0
    sha = site["git"](site["checkout"], "rev-parse", "--short", "HEAD")
    r = _wolfctl(site, "status", cwd=site["tmp"])
    assert f"Code: feat/x @ {sha}, 1 ahead of origin/main" in r.stdout


def test_usage_and_version(site):
    out = _wolfctl(site, "help").stdout
    assert "wolfctl update --branch NAME" in out and "4 the checkout has local changes" in out
    assert "wolfctl v3.28" in _wolfctl(site, "version").stdout


def test_wolfctl_syntax_valid():
    assert subprocess.run(["bash", "-n", _WOLFCTL]).returncode == 0
