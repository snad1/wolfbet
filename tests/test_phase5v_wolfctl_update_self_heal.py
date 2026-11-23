"""Phase 5v: wolfctl v3.7 self-heal + CLI restart + wizard banner (v2.30.2).

Three defects (v2.30.1 era):
  A) `wolfctl update` couldn't self-upgrade old (v3.1) installs: no drift
     warning surfaced so operators saw "Already up to date" while their
     installed dispatcher was stale.
  B) `wolfctl update` refreshed wolfbet.py on disk but never restarted the
     CLI daemon, so the running process kept the old code.
  C) `setup_wizard()` banner hardcoded "WolfBet AutoBot v2.6" instead of
     using the current VERSION.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5v_wolfctl_update_self_heal.py -q
"""

import inspect
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


_WOLFCTL_PATH = os.path.join(_WOLFBET_DIR, "wolfctl")


def _wolfctl_src() -> str:
    with open(_WOLFCTL_PATH) as f:
        return f.read()


# ── Version bumps ────────────────────────────────────────
def test_wolfbet_version_bumped():
    assert wb.VERSION == "2.50.0"


def test_wolfctl_version_constant_current():
    src = _wolfctl_src()
    # Test the SHAPE of the constant, not a specific version (avoids
    # test churn on every wolfctl bump).
    assert re.search(r"^WOLFCTL_VERSION=\d+\.\d+\b", src, re.MULTILINE), \
        "WOLFCTL_VERSION=X.Y constant is missing"


def test_wolfctl_header_and_usage_bumped_to_3_8():
    src = _wolfctl_src()
    assert "Management Command v3.24" in src
    assert "WolfBet AutoBot Manager v3.24" in src
    assert "wolfctl v3.24" in src
    assert "v3.7" not in src, "stale v3.7 references remain"


# ── Defect A: drift warning ──────────────────────────────
def test_wolfctl_update_has_drift_warning():
    src = _wolfctl_src()
    assert "wolfctl was stale" in src, "drift warning marker missing"
    assert "exec wolfctl update" in src, "escape-hatch one-liner missing"


def test_wolfctl_update_reads_repo_version_from_script():
    src = _wolfctl_src()
    # Drift check compares WOLFCTL_VERSION (running interpreter) against the
    # repo copy pulled from the current SCRIPT_NAME file.
    assert "grep -m1 '^WOLFCTL_VERSION=' \"$SCRIPT_NAME\"" in src \
        or "grep -m1 \"^WOLFCTL_VERSION=\" \"$SCRIPT_NAME\"" in src


# ── Defect B: CLI restart on update ──────────────────────
def test_wolfctl_update_restarts_cli_daemon():
    src = _wolfctl_src()
    # Two systemctl restart blocks now: one for $TG_SERVICE, one for $SERVICE.
    assert 'systemctl --user restart "$SERVICE"' in src, \
        "CLI daemon restart block missing"
    assert src.count('systemctl --user is-active --quiet "$SERVICE"') >= 1


def test_wolfctl_update_cli_restart_prints_success_message():
    src = _wolfctl_src()
    assert "CLI daemon restarted" in src


# ── Defect C: wizard banner uses VERSION ─────────────────
def test_wizard_banner_no_longer_hardcoded_v2_6():
    src = inspect.getsource(wb)
    assert "WolfBet AutoBot  v2.6[/]" not in src, \
        "stale v2.6 wizard-banner literal still present"


def test_wizard_banner_uses_VERSION():
    src = inspect.getsource(wb)
    assert 'f"[bold cyan]WolfBet AutoBot  v{VERSION}[/]' in src, \
        "wizard banner must interpolate VERSION"


# ── Defensive: every subcommand in help maps to a case handler ─
def test_wolfctl_help_subcommands_all_dispatch():
    """Regression lock: prevent a v3.1-style shipping-old-dispatcher recurrence.
    Every command advertised in `usage()` must have a matching case branch."""
    src = _wolfctl_src()
    # Grab advertised subcommand names from `wolfctl X ...` lines in usage.
    advertised = set()
    for m in re.finditer(r"^\s*echo\s+\"    wolfctl\s+([a-z][a-z0-9-]*)",
                         src, re.MULTILINE):
        advertised.add(m.group(1))
    # Grab dispatched cases. Two shapes exist in wolfctl:
    #   `    name)       cmd_name ;;`
    #   `    name)       shift; cmd_name "$@" ;;`
    dispatched = set()
    for m in re.finditer(r"^\s+([a-z][a-z0-9-]*(?:\|[a-z0-9-]+)*)\)\s*(?:shift;\s*)?cmd_",
                         src, re.MULTILINE):
        for alias in m.group(1).split("|"):
            dispatched.add(alias)
    # `tg` and `web` are subcommand dispatchers (their sub-sub commands are
    # nested inside cmd_tg / cmd_web, not top-level cases).
    missing = advertised - dispatched - {"tg", "web"}
    assert not missing, f"advertised but not dispatched: {missing}"
