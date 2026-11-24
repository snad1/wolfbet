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


# ── key verbs for the dashboard: JSON out, the secret only ever on stdin ──
FRIEND_KEY = "wb_live_FRIEND_0123456789abcdef7777"


def _key(monkeypatch, capsys, action, key_id=None, secret=None, **kw):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("" if secret is None else secret + "\n"))
    code = 0
    try:
        wb.cmd_key(action, key_id, **kw)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return json.loads(out), code, out


@pytest.fixture
def lanes_cfg(monkeypatch, tmp_path):
    return _config(monkeypatch, tmp_path, {**BASE, "lanes": {"count": 2}})


def test_key_put_reads_stdin_and_never_prints_the_secret(monkeypatch, capsys, lanes_cfg):
    doc, code, raw = _key(monkeypatch, capsys, "put", "friend-a", FRIEND_KEY,
                          label="Friend A", owner=7)
    assert code == 0 and FRIEND_KEY not in raw
    assert doc["key"] == {**doc["key"], "id": "friend-a", "label": "Friend A", "last4": "7777",
                          "owner_user_id": 7, "used_by_lanes": []}
    assert Vault("wolfbet").secret("friend-a") == FRIEND_KEY


def test_key_list_shows_which_lanes_use_each_key(monkeypatch, capsys, lanes_cfg):
    Vault("wolfbet").put("default", ENV_KEY, "Main")
    Vault("wolfbet").put("friend-a", FRIEND_KEY, "Friend A")
    doc, code, raw = _key(monkeypatch, capsys, "list")
    assert code == 0 and ENV_KEY not in raw and FRIEND_KEY not in raw
    assert [(k["id"], k["used_by_lanes"]) for k in doc["keys"]] == [
        ("default", [1, 2]), ("friend-a", [])]


@pytest.mark.parametrize("action,key_id,secret,code", [
    ("put", "friend-a", FRIEND_KEY, "key_exists"),
    ("put", "new", "short", "invalid_secret"),
    ("put", "new", "has space in it", "invalid_secret"),
    ("rotate", "nobody", FRIEND_KEY, "key_unknown"),
    ("delete", "default", None, "key_in_use"),
    ("delete", "../x", None, "invalid_argument"),
])
def test_refused_key_changes_leave_the_vault_alone(monkeypatch, capsys, lanes_cfg,
                                                   action, key_id, secret, code):
    vault = Vault("wolfbet")
    vault.put("default", ENV_KEY, "Main")
    vault.put("friend-a", FRIEND_KEY, "Friend A")
    before = [(k.key_id, k.last4, k.rotated_at) for k in vault.keys()]
    doc, exit_code, raw = _key(monkeypatch, capsys, action, key_id, secret)
    assert exit_code == 2 and doc["error"]["code"] == code
    assert [(k.key_id, k.last4, k.rotated_at) for k in vault.keys()] == before
    assert "short" not in raw or code != "invalid_secret"
    if code == "key_in_use":
        assert doc["error"]["message"] == ("This key is used by lane 1, 2. "
                                           "Move the lanes to another key first.")


def test_key_rotate_keeps_label_and_owner_and_says_when_it_applies(monkeypatch, capsys, lanes_cfg):
    Vault("wolfbet").put("friend-a", FRIEND_KEY, "Friend A", owner_user_id=7)
    doc, code, _ = _key(monkeypatch, capsys, "rotate", "friend-a", "wb_live_ROTATED_000000008888")
    assert code == 0 and doc["key"]["last4"] == "8888" and doc["key"]["rotated_at"]
    assert (doc["key"]["label"], doc["key"]["owner_user_id"]) == ("Friend A", 7)
    assert doc["note"] == "Lanes using this key pick up the new one when they restart."


def test_rotating_default_warns_while_the_env_key_is_set(monkeypatch, capsys, lanes_cfg):
    Vault("wolfbet").put("default", ENV_KEY, "Main")
    monkeypatch.setenv("WOLFBET_API_KEY", ENV_KEY)
    doc, _, _ = _key(monkeypatch, capsys, "rotate", "default", FRIEND_KEY)
    assert "remove it from .env" in doc["note"]


def test_an_unused_key_is_deleted(monkeypatch, capsys, lanes_cfg):
    Vault("wolfbet").put("friend-a", FRIEND_KEY, "Friend A")
    doc, code, _ = _key(monkeypatch, capsys, "delete", "friend-a")
    assert code == 0 and doc == {"v": 1, "deleted": "friend-a"}
    assert Vault("wolfbet").info("friend-a") is None


def test_key_verbs_without_a_vault_module_say_so(monkeypatch, capsys, lanes_cfg):
    monkeypatch.setattr(wb, "_vault", lambda: None)
    doc, code, _ = _key(monkeypatch, capsys, "list")
    assert code == 1 and doc["error"]["code"] == "vault_unavailable"
