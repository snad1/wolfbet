"""The lane registry, session-end requests and vault reads come from casino-shared."""

import os
import shutil
import subprocess
import sys

_WOLFBET_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _WOLFBET_DIR)
os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from shared.core import lanes, vault  # noqa: E402


def test_the_bot_uses_the_shared_helpers():
    assert wb._lane_registry is lanes.lane_registry
    assert wb._lanes_count is lanes.lanes_count
    assert wb.LANES_MAX_DEFAULT == lanes.LANES_MAX and wb.LANE_ID_MAX == lanes.LANE_ID_MAX
    assert wb.vault_secret is vault.vault_secret


def test_an_older_casino_shared_is_named_instead_of_a_traceback(tmp_path):
    real = os.path.dirname(lanes.__file__.rsplit(os.sep, 1)[0])
    shutil.copytree(real, tmp_path / "shared", ignore=shutil.ignore_patterns("tests", ".*"))
    with open(tmp_path / "shared" / "core" / "lanes.py", "a") as f:
        f.write("\ndel lane_registry\n")
    env = {**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{_WOLFBET_DIR}", "APP_ENV": "testing"}
    r = subprocess.run([sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"), "--help"],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stderr.strip() == "casino-shared 0.9.0 or newer is needed: run wolfctl update"
