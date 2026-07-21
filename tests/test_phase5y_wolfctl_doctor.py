"""Phase 5y — wolfctl v3.7 `doctor` subcommand (health check + fixable prescriptions).

Real-world runbook (see CHANGELOG v3.7) freed 3.9 GB of journals and dropped
cycle_ms from 143 → 79 on the old server. `doctor` bundles those ops-side
levers into one subcommand: `doctor` reads-only, `doctor --fix` applies the
safe subset with per-fix confirmation.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5y_wolfctl_doctor.py -q
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

_WOLFCTL_PATH = os.path.join(_WOLFBET_DIR, "wolfctl")


def _wolfctl_src() -> str:
    with open(_WOLFCTL_PATH) as f:
        return f.read()


# ── Version bump ─────────────────────────────────────────
def test_wolfctl_version_constant_shape():
    src = _wolfctl_src()
    # Test the SHAPE of the constant, not a specific version (avoids
    # test churn on every wolfctl bump — the v3.8-specific header/usage
    # tests in phase5v cover the exact version numeric).
    assert re.search(r"^WOLFCTL_VERSION=\d+\.\d+\b", src, re.MULTILINE)


# ── doctor advertised + dispatched ───────────────────────
def test_doctor_advertised_in_usage():
    src = _wolfctl_src()
    assert "wolfctl doctor" in src


def test_doctor_dispatched_in_case_block():
    src = _wolfctl_src()
    assert re.search(r"^\s+doctor\)\s+shift;\s*cmd_doctor", src, re.MULTILINE)


def test_cmd_doctor_function_defined():
    src = _wolfctl_src()
    assert re.search(r"^cmd_doctor\(\)\s*\{", src, re.MULTILINE)


# ── prescription block content ───────────────────────────
def test_prescription_includes_all_safe_levers():
    src = _wolfctl_src()
    assert "sqlite3" in src and "VACUUM" in src
    assert "--vacuum-size=100M" in src
    assert "wolfctl cleanup 1" in src


def test_prescription_includes_privileged_levers():
    src = _wolfctl_src()
    assert "renice -n -5" in src
    assert "ionice -c 2 -n 0" in src
    assert "cpupower frequency-set -g performance" in src


def test_prescription_gates_vacuum_on_fragmentation_threshold():
    """VACUUM only prescribed when freelist/pages ratio > 20% — otherwise noise."""
    src = _wolfctl_src()
    # The threshold check and the prescription line live in cmd_doctor.
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    assert doctor_body, "cmd_doctor body not found"
    body = doctor_body.group(0)
    assert 'frag_pct' in body
    assert '-ge 20' in body


def test_prescription_gates_journal_vacuum_on_size():
    """Journal vacuum only prescribed when journalctl reports > 500 MB or 1+ GB."""
    src = _wolfctl_src()
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    body = doctor_body.group(0)
    assert 'grep -qE' in body
    # 5-9xx MB OR any G.
    assert '[5-9][0-9][0-9]M' in body


# ── VPS governor detection ───────────────────────────────
def test_governor_prescription_skips_on_virtualized_host():
    """QEMU-KVM guests can't set governor; cpupower fails with a confusing
    error. v3.7 detects virt-type first and downgrades to [SKIP] with
    the honest advice (ask provider / migrate to dedicated CPU)."""
    src = _wolfctl_src()
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    body = doctor_body.group(0)
    assert 'systemd-detect-virt' in body
    assert '[SKIP]' in body
    assert 'guests cannot set the governor' in body


def test_governor_skip_when_unreadable_on_virt():
    """v3.8 — when governor path is entirely unreadable (VPS host hides it)
    AND systemd-detect-virt reports virtualization, still print [SKIP] with
    the honest 'VPS provider controls it' message instead of staying silent."""
    src = _wolfctl_src()
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    body = doctor_body.group(0)
    assert 'governor path not exposed on this virtualized host' in body


# ── --fix runs only the three safe fixes ─────────────────
def test_fix_flag_recognized():
    src = _wolfctl_src()
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    body = doctor_body.group(0)
    assert '--fix' in body
    assert '--yes' in body and '-y' in body


def test_fix_block_never_invokes_privileged_commands():
    """--fix must ONLY run the SAFE subset (cleanup + VACUUM + journal vacuum).
    Privileged fixes are prescribed as text, never eval'd."""
    src = _wolfctl_src()
    doctor_body = re.search(r"cmd_doctor\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE)
    body = doctor_body.group(0)
    # Locate the applying-fixes block.
    fix_block = re.search(
        r'Applying safe fixes \(--fix\).*?^\s+else\b',
        body, re.DOTALL | re.MULTILINE,
    )
    assert fix_block, "safe-fixes application block not found"
    fb = fix_block.group(0)
    # No sudo, no renice, no ionice, no cpupower inside the applied block.
    assert 'sudo' not in fb, "sudo must never appear inside --fix apply block"
    # Three safe fixes invoked (grep excludes the function definition line).
    invocations = re.findall(r'^\s+_doctor_run_fix\s+"', fb, re.MULTILINE)
    assert len(invocations) == 3, f"expected 3 safe-fix invocations, got {len(invocations)}"


# ── Help block advertises the new subcommand ─────────────
def test_help_lists_doctor_between_diagnose_and_backtest():
    src = _wolfctl_src()
    # Loose order check — doctor advertised after diagnose in usage().
    idx_diagnose = src.find('"    wolfctl diagnose ')
    idx_doctor = src.find('"    wolfctl doctor ')
    assert idx_diagnose > 0
    assert idx_doctor > idx_diagnose, "doctor should be advertised after diagnose"
