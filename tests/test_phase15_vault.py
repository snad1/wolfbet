"""The API key moves from plaintext files into the host key vault.

WOLFBET_API_KEY wins while set and keeps the vault's `default` equal to it; a key only
in the config file is moved in. The config loses its copy (one 0600 backup kept) only
once the vault returns the same key; a broken vault leaves everything as it was.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase15_vault.py -q
"""

import json
import logging
import os
import stat
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from shared.core.vault import Vault  # noqa: E402

ENV_KEY = "wb_live_ENVKEY_0123456789abcdef1111"
FILE_KEY = "wb_live_FILEKEY_0123456789abcdef2222"
BASE = {"api_key": FILE_KEY, "currency": "usdt", "game": "dice", "rule": "over",
        "multiplier": 2.0, "bet_value": 50.49, "base_bet": 0.0001,
        "strategy": "Flat", "strategy_key": "1"}


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    saved = (wb.state.api_key, wb.state.lane)
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.delenv("WOLFBET_API_KEY", raising=False)
    yield
    wb.state.api_key, wb.state.lane = saved


def _config(monkeypatch, tmp_path, data=None):
    path = str(tmp_path / "cfg.json")
    with open(path, "w") as f:
        json.dump(dict(BASE) if data is None else data, f)
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    wb.state.lane = 1
    return path


def _saved(path) -> dict:
    return json.load(open(path))


def _vault_key():
    return Vault("wolfbet").secret("default")


def _start():
    assert wb.load_config()
    wb._sync_vault_key()


def test_env_key_moves_into_the_vault_and_leaves_the_config(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    original = open(path).read()
    monkeypatch.setenv("WOLFBET_API_KEY", ENV_KEY)
    _start()
    assert wb.state.api_key == ENV_KEY and _vault_key() == ENV_KEY
    assert "api_key" not in _saved(path)
    backup = path + ".pre-vault"
    assert open(backup).read() == original
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600


def test_a_changed_env_key_replaces_the_vault_copy(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path)
    monkeypatch.setenv("WOLFBET_API_KEY", ENV_KEY)
    _start()
    monkeypatch.setenv("WOLFBET_API_KEY", "wb_live_NEWKEY_0123456789abcdef3333")
    _start()
    info = Vault("wolfbet").info("default")
    assert _vault_key() == "wb_live_NEWKEY_0123456789abcdef3333" and info.rotated_at


def test_a_key_only_in_the_config_is_moved_in(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    _start()
    assert wb.state.api_key == FILE_KEY and _vault_key() == FILE_KEY
    assert "api_key" not in _saved(path)


def test_a_later_start_bets_with_the_vault_key_alone(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    _start()
    wb.state.api_key = ""
    assert wb.load_config()
    assert wb.state.api_key == FILE_KEY and "api_key" not in _saved(path)


def test_the_backup_is_written_once(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    original = open(path).read()
    _start()
    with open(path, "w") as f:
        json.dump({**BASE, "api_key": "wb_live_LATER_0123456789abcdef4444"}, f)
    _start()
    assert open(path + ".pre-vault").read() == original


def test_a_broken_vault_falls_back_and_strips_nothing(monkeypatch, tmp_path, caplog):
    path = _config(monkeypatch, tmp_path)
    monkeypatch.setenv("WOLFBET_API_KEY", ENV_KEY)
    kek = os.environ["CASINO_VAULT_KEY_FILE"]
    os.makedirs(os.path.dirname(kek), mode=0o700)
    with open(kek, "wb") as f:
        f.write(os.urandom(32))
    os.chmod(kek, 0o644)
    with caplog.at_level(logging.WARNING, logger=wb.logger.name):
        _start()
    assert wb.state.api_key == ENV_KEY
    assert _saved(path)["api_key"] == FILE_KEY
    assert not os.path.exists(path + ".pre-vault")
    assert "Key vault unavailable" in caplog.text


def test_lanes_above_one_only_read(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    wb.state.lane = 2
    assert wb.load_config()
    wb.state.lane = 2
    wb._sync_vault_key()
    assert wb.state.api_key == FILE_KEY
    assert _saved(path)["api_key"] == FILE_KEY and _vault_key() is None


def test_read_only_commands_write_nothing(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    before = open(path).read()
    wb._config_show_data()
    assert open(path).read() == before and _vault_key() is None


def test_save_config_keeps_the_key_out_of_the_file(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    assert wb.load_config()
    wb.state.api_key = ENV_KEY
    wb.save_config()
    assert "api_key" not in _saved(path) and _vault_key() == ENV_KEY


def test_save_config_keeps_the_key_in_the_file_when_the_vault_is_down(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path)
    assert wb.load_config()
    monkeypatch.setattr(wb, "_vault", lambda: None)
    wb.save_config()
    assert _saved(path)["api_key"] == FILE_KEY


def test_the_key_never_reaches_the_log(monkeypatch, tmp_path, caplog):
    _config(monkeypatch, tmp_path)
    monkeypatch.setenv("WOLFBET_API_KEY", ENV_KEY)
    with caplog.at_level(logging.DEBUG, logger=wb.logger.name):
        _start()
        wb.save_config()
    text = caplog.text
    assert "API key stored in the vault as default" in text
    for key in (ENV_KEY, FILE_KEY):
        assert key not in text and key[8:-4] not in text
