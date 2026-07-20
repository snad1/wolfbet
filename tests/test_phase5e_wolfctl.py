"""Phase 5e tests — wolfctl shell wrapper (subcommand parity + install.sh drift fix).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5e_wolfctl.py -q

wolfctl is a bash script; these tests are string-presence + subprocess smoke
checks. Every new subcommand is verified against a stub PYTHON/BOT so we
never touch the real daemon.
"""

import os
import stat
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
_WOLFCTL = os.path.join(_WOLFBET_DIR, "wolfctl")
_INSTALL_SH = os.path.join(_WOLFBET_DIR, "install.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ── Coverage: every new cmd_* function is present ─────────
def test_wolfctl_has_new_cmd_functions():
    src = _read(_WOLFCTL)
    for fn in ("cmd_config_show", "cmd_config_set", "cmd_analytics",
               "cmd_backtest", "cmd_export", "cmd_last_bets",
               "cmd_preset", "cmd_version", "cmd_pipeline"):
        assert f"{fn}()" in src, f"missing function {fn}() in wolfctl"


# ── Coverage: every new dispatch case is present ──────────
def test_wolfctl_dispatch_cases():
    src = _read(_WOLFCTL)
    for case in ("config-show)", "config-set)", "analytics)", "last-bets)",
                 "export)", "pipeline)", "backtest)", "preset)"):
        assert case in src, f"missing dispatch case {case!r}"
    # version has alias-form dispatch: `version|--version|-v)`
    assert "version|--version|-v)" in src


# ── Coverage: every new flag string appears in wolfctl ────
def test_wolfctl_wires_expected_flags():
    src = _read(_WOLFCTL)
    for flag in ("--config-show", "--config-set", "--analytics",
                 "--backtest", "--export", "--export-session-id",
                 "--last-bets", "--preset", "--version"):
        assert flag in src, f"missing flag {flag} in wolfctl"


# ── Version bump v3.2 → v3.3 ──────────────────────────────

# ── usage() coverage floor ────────────────────────────────
def test_wolfctl_usage_lists_new_subcommands():
    """Run `bash wolfctl` (no args) → usage output. Count expected subcommand rows."""
    proc = subprocess.run(["bash", _WOLFCTL], capture_output=True, text=True, timeout=10)
    out = proc.stdout
    for sub in ("config-show", "config-set", "analytics", "backtest",
                "export", "last-bets", "preset", "pipeline", "version"):
        assert sub in out, f"usage() missing {sub!r}"


def test_wolfctl_usage_row_count():
    """Grep '^    wolfctl ' — every advertised subcommand is one row."""
    proc = subprocess.run(["bash", _WOLFCTL], capture_output=True, text=True, timeout=10)
    rows = [ln for ln in proc.stdout.splitlines() if ln.startswith("    wolfctl ")]
    assert len(rows) >= 26, f"expected >= 26 subcommand rows, got {len(rows)}"


# ── Argument validation — missing args exit non-zero ─────
def _run_wolfctl(*args, path_stub_dir: str = None):
    """Run wolfctl with a stubbed PYTHON+BOT so it never invokes the real daemon.

    We stub by placing dummy paths that don't exist; wolfctl argument-validation
    branches exit BEFORE reaching PYTHON, so validation still works.
    """
    env = os.environ.copy()
    if path_stub_dir:
        # Point INSTALL_DIR to a nonexistent path so any Python exec would fail
        # (we only want to trigger the validation branch).
        env["HOME"] = path_stub_dir
    return subprocess.run(["bash", _WOLFCTL, *args], capture_output=True, text=True, timeout=10, env=env)


def test_config_set_no_args_prints_usage():
    with tempfile.TemporaryDirectory() as td:
        p = _run_wolfctl("config-set", path_stub_dir=td)
        assert p.returncode == 2
        assert "Usage: wolfctl config-set" in p.stdout + p.stderr


def test_pipeline_no_args_prints_usage():
    with tempfile.TemporaryDirectory() as td:
        p = _run_wolfctl("pipeline", path_stub_dir=td)
        assert p.returncode == 2
        assert "Usage: wolfctl pipeline" in p.stdout + p.stderr


def test_export_no_args_prints_usage():
    with tempfile.TemporaryDirectory() as td:
        p = _run_wolfctl("export", path_stub_dir=td)
        assert p.returncode == 2
        assert "Usage: wolfctl export" in p.stdout + p.stderr


def test_preset_no_args_prints_usage():
    with tempfile.TemporaryDirectory() as td:
        p = _run_wolfctl("preset", path_stub_dir=td)
        assert p.returncode == 2
        assert "Usage: wolfctl preset" in p.stdout + p.stderr


# ── install.sh drift fix ─────────────────────────────────
def test_install_sh_removed_heredoc():
    src = _read(_INSTALL_SH)
    # Old inlined v3.1 header must be gone.
    assert "Unified Management Command v3.1" not in src
    # The heredoc marker itself must be gone.
    assert "cat > \"$WOLFCTL\" << 'CTLEOF'" not in src


def test_install_sh_uses_install_command():
    src = _read(_INSTALL_SH)
    assert "install -m 0755" in src
    assert "SCRIPT_DIR/wolfctl" in src


def test_install_sh_syntax_valid():
    proc = subprocess.run(["bash", "-n", _INSTALL_SH], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr


# ── wolfctl syntax valid ─────────────────────────────────
def test_wolfctl_syntax_valid():
    proc = subprocess.run(["bash", "-n", _WOLFCTL], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr


# ── wolfctl --help / help / -h alias ─────────────────────
def test_wolfctl_help_alias():
    proc = subprocess.run(["bash", _WOLFCTL, "help"], capture_output=True, text=True, timeout=5)
    assert "wolfctl —" in proc.stdout or "wolfctl" in proc.stdout


def test_wolfctl_version_v3_6():
    src = _read(_WOLFCTL)
    assert "v3.6" in src
    assert "v3.5" not in src


def test_wolfctl_has_benchmark_diagnose_subcommands():
    src = _read(_WOLFCTL)
    for token in ("cmd_benchmark()", "cmd_diagnose()", "benchmark)", "diagnose)",
                  "--benchmark", "--diagnose"):
        assert token in src, f"missing {token!r} in wolfctl"


def test_wolfctl_has_cleanup_subcommand():
    src = _read(_WOLFCTL)
    assert "cmd_cleanup()" in src
    assert "cleanup)" in src
    assert "--cleanup" in src


def test_wolfctl_uses_https_for_casino_shared():
    src = _read(_WOLFCTL)
    assert "git+https://github.com/snad1/casino-shared" in src
    assert "git+ssh://" not in src


def test_update_uses_autostash():
    """cmd_update must pass --autostash so a dirty tree doesn't abort the pull."""
    src = _read(_WOLFCTL)
    assert "pull --ff-only --autostash" in src


def test_update_prints_status_on_dirty_tree():
    """User must see WHAT got autostashed before the pull runs."""
    src = _read(_WOLFCTL)
    assert "Local changes detected" in src
    assert "git status --porcelain" in src


def test_update_prints_recovery_hint_on_failure():
    """On pull failure, tell the user how to recover their stashed changes."""
    src = _read(_WOLFCTL)
    assert "git stash list" in src
    assert "git stash pop" in src


def test_wolfctl_version_alias():
    """--version / -v / version all route to cmd_version.

    cmd_version invokes PYTHON which won't exist in the test env; we only
    care that the dispatch case exists (checked separately) and that the
    header comment reflects v3.3 (checked separately). This test asserts
    the case-statement includes the three aliases.
    """
    src = _read(_WOLFCTL)
    assert "version|--version|-v" in src
