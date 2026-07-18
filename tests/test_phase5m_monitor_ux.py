"""Phase 5m tests — monitor UX + systemd Restart= migration (v2.28.1).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5m_monitor_ux.py -q
"""

import inspect
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


_INSTALL_SH = os.path.join(_WOLFBET_DIR, "install.sh")
_WOLFCTL    = os.path.join(_WOLFBET_DIR, "wolfctl")


# ── Monitor footer + attach banner ────────────────────────
def test_monitor_screen_lists_working_keys():
    """The monitor screen must advertise every key it actually handles."""
    src = inspect.getsource(wb._build_monitor_screen)
    for token in ("[P]ause", "[R]esume", "[Q]uit", "[S]top"):
        assert token in src, f"missing {token!r} in _build_monitor_screen"


def test_monitor_screen_advertises_wolfctl_config_set():
    """Users pressing T/E/H should see the correct alternative right in the footer."""
    src = inspect.getsource(wb._build_monitor_screen)
    assert "wolfctl config-set" in src


def test_monitor_screen_advertises_export_via_wolfctl():
    src = inspect.getsource(wb._build_monitor_screen)
    assert "wolfctl export" in src


def test_cmd_monitor_attach_banner_mentions_wolfctl_config_set():
    src = inspect.getsource(wb.cmd_monitor)
    assert "wolfctl config-set" in src
    assert "Keys:" in src


# ── _is_systemd_managed helper ────────────────────────────
def test_is_systemd_managed_none_pid():
    assert wb._is_systemd_managed(None) is False
    assert wb._is_systemd_managed(0) is False


def test_is_systemd_managed_true(monkeypatch):
    """Simulate a systemd-managed PID via a temp cgroup file."""
    with tempfile.TemporaryDirectory() as td:
        proc_dir = os.path.join(td, "proc", "12345")
        os.makedirs(proc_dir)
        with open(os.path.join(proc_dir, "cgroup"), "w") as f:
            f.write("0::/user.slice/user-0.slice/user@0.service/app.slice/wolfbet.service\n")
        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/proc/12345/cgroup":
                return real_open(os.path.join(proc_dir, "cgroup"), *a, **kw)
            return real_open(path, *a, **kw)

        import builtins
        monkeypatch.setattr(builtins, "open", fake_open)
        assert wb._is_systemd_managed(12345) is True


def test_is_systemd_managed_false(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        proc_dir = os.path.join(td, "proc", "54321")
        os.makedirs(proc_dir)
        with open(os.path.join(proc_dir, "cgroup"), "w") as f:
            f.write("0::/user.slice/user-0.slice/some-other.service\n")
        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/proc/54321/cgroup":
                return real_open(os.path.join(proc_dir, "cgroup"), *a, **kw)
            return real_open(path, *a, **kw)

        import builtins
        monkeypatch.setattr(builtins, "open", fake_open)
        assert wb._is_systemd_managed(54321) is False


def test_is_systemd_managed_missing_proc(monkeypatch):
    """Non-Linux hosts or missing /proc entries must return False, not raise."""
    import builtins
    real_open = open

    def fake_open(path, *a, **kw):
        if path.startswith("/proc/"):
            raise OSError("no such file")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert wb._is_systemd_managed(99999) is False


# ── cmd_monitor stop path prefers systemctl ───────────────
def test_cmd_monitor_stop_goes_through_systemctl_when_managed():
    src = inspect.getsource(wb.cmd_monitor)
    assert "systemctl" in src
    assert "_is_systemd_managed" in src


# ── install.sh unit policy ───────────────────────────────
def test_install_sh_uses_restart_on_failure():
    src = _read(_INSTALL_SH)
    # Exactly two service files use it (CLI + TG).
    assert src.count("Restart=on-failure") >= 2
    # Old always policy must be gone.
    assert "Restart=always" not in src


# ── wolfctl migration branch ─────────────────────────────
def test_wolfctl_has_restart_migration_branch():
    src = _read(_WOLFCTL)
    assert "Restart=always" in src            # for the grep -q + sed match
    assert "Restart=on-failure" in src        # target value
    assert "Migrated" in src                  # user-facing confirmation


def test_wolfctl_migration_reloads_systemd():
    src = _read(_WOLFCTL)
    assert "systemctl --user daemon-reload" in src


def test_wolfctl_migration_uses_sed_backup_safe():
    """Migration writes a .bak and removes it — atomic-ish + no artefacts left."""
    src = _read(_WOLFCTL)
    assert "sed -i.bak" in src
    assert "rm -f \"$_unit_path.bak\"" in src or "rm -f $_unit_path.bak" in src


# ── Bash syntax valid after edits ─────────────────────────
def test_install_sh_syntax_valid():
    import subprocess
    proc = subprocess.run(["bash", "-n", _INSTALL_SH],
                          capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr


def test_wolfctl_syntax_valid():
    import subprocess
    proc = subprocess.run(["bash", "-n", _WOLFCTL],
                          capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
