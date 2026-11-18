"""--config-schema and --config-set --dry-run.

The schema is built from the same tables config-set validates with, and a dry
run goes through the same validation as the real set, writing and signalling
nothing. The real set now validates every pair before it writes anything.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11h_config_schema.py -q
"""

import hashlib
import io
import json
import os
import signal
import sys

import pytest
from rich.console import Console

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
for _p in (_WOLFBET_DIR, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from test_phase11g_json_outputs import _CONFIG, SECRETS  # noqa: E402

_FIXTURES = os.path.join(_HERE, "fixtures", "cli_v1")
_SNAPSHOTS = os.path.join(_FIXTURES, "snapshots")
DAEMON_PID = 424242
LANE2_PID = 424243


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Saved config, pid files for lanes 1 and 2, a fake os.kill, a fresh BotState, a fixed console."""
    monkeypatch.chdir(tmp_path)
    with open("config.json", "w") as f:
        json.dump(_CONFIG, f, indent=2)
    with open("wb.pid", "w") as f:
        f.write(str(DAEMON_PID))
    with open(wb._lane_path("wb.pid", 2), "w") as f:
        f.write(str(LANE2_PID))
    kills = []
    monkeypatch.setattr(wb.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(wb, "CONFIG_PATH", "config.json")
    monkeypatch.setattr(wb, "PID_PATH", "wb.pid")
    monkeypatch.setattr(wb, "_BASE_PID_PATH", "wb.pid")
    monkeypatch.setattr(wb, "STATE_PATH", "live.json")
    monkeypatch.setattr(wb, "state", wb.BotState())
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.delenv("WOLFBET_API_KEY", raising=False)
    monkeypatch.delenv("WOLFBET_EVENTS_URL", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(wb, "console", Console(file=buf, width=160, color_system=None,
                                               force_terminal=False, legacy_windows=False))
    return {"buf": buf, "kills": kills}


_SET_CASES = {
    "set_live_clamped":   ["maxloss=0.02", "recdelay=99999", "delay=0.5", "sdloss=4:2"],
    "set_restart":        ["multiplier=3", "strategy=1", "piscope=run"],
    "set_secrets":        ["proxy=http://u2:p2secret@10.0.0.2:3128",
                           "events_url=redis://x:pw2secret@127.0.0.1:6380/1"],
    "set_lanes":          ["lanes=4", "lane_split=budget", "lane2.multiplier=default",
                           "lane3.maxwins=5"],
    "set_mixed":          ["lane2.maxloss=0.003", "maxbets=100"],
    "err_unknown_key":    ["foo=1"],
    "err_bad_value":      ["maxloss=abc"],
    "err_lane_forbidden": ["lane2.proxy=http://10.0.0.9:80"],
    "err_api_key":        ["api_key=zzz"],
    "err_lanes_count":    ["lanes=99"],
    "err_malformed":      ["maxloss"],
}


def _run_real_set(env, pairs) -> str:
    """Real config-set: Rich output, exit code, then the config file and the signals sent."""
    code = 0
    try:
        wb.cmd_config_set(list(pairs))
    except SystemExit as e:
        code = e.code
    with open("config.json") as f:
        saved = f.read()
    return (env["buf"].getvalue() + f"[exit {code}]\n" + saved
            + f"\n[signals {sorted(env['kills'])}]\n")


@pytest.mark.parametrize("case", sorted(_SET_CASES))
def test_real_config_set_output_unchanged(env, case):
    with open(os.path.join(_SNAPSHOTS, f"{case}.txt"), encoding="utf-8") as f:
        assert _run_real_set(env, _SET_CASES[case]) == f.read()


# ── --config-schema ──
def _validate(doc: dict, name: str) -> None:
    import jsonschema
    with open(os.path.join(_FIXTURES, f"wolfbet-cli-{name}-v1.json")) as f:
        jsonschema.Draft202012Validator(json.load(f)).validate(doc)


def _schema_doc(capsys) -> dict:
    wb._print_json(wb._config_schema_data())
    doc = json.loads(capsys.readouterr().out)
    _validate(doc, "config-schema")
    return doc


def test_schema_lists_exactly_the_keys_config_set_accepts(env, capsys):
    keys = {k["key"]: k for k in _schema_doc(capsys)["keys"]}
    assert set(keys) == wb._TWEAK_PARAMS
    assert set(wb._CONFIG_VALUE_RULES) == wb._TWEAK_PARAMS
    for alias, canonical in wb._TWEAK_ALIAS.items():
        assert alias in keys[canonical]["aliases"]
        assert wb._parse_config_set_pairs([f"{alias}=1"]) == {canonical: "1"}
    for key in keys:
        assert wb._parse_config_set_pairs([f"{key}=1"]) == {key: "1"}
    with pytest.raises(ValueError, match="Unknown config field"):
        wb._parse_config_set_pairs(["not_a_key=1"])


def test_schema_flags_match_the_reload_and_lane_tables(env, capsys):
    for k in _schema_doc(capsys)["keys"]:
        assert k["live_reload"] == (k["key"] in wb._LIVE_RELOAD_KEYS)
        assert k["lane_overridable"] == (k["key"] not in wb._LANE_FORBIDDEN_KEYS)
        assert k["help"] == dict(wb._CONFIG_SHOW_FIELDS)[k["key"]]


def test_schema_defaults_and_lanes(env, capsys):
    doc = _schema_doc(capsys)
    keys = {k["key"]: k for k in doc["keys"]}
    assert keys["recurring_delay"]["default"] == 60.0
    assert keys["streakbet_loss"]["default"] == [0, 1.0]
    assert keys["proxy"]["default"] is None
    assert keys["strategy_key"]["aliases"] == ["strategy"]
    assert doc["lanes"] == {"count": {"min": 1, "max": wb.LANES_MAX},
                            "split": {"choices": list(wb.LANE_SPLIT_MODES)},
                            "override_lanes": {"min": 2, "max": wb.LANES_MAX},
                            "unset_values": ["default", "unset", "inherit"]}


def _dry(env, capsys, *pairs):
    """Run a JSON dry run; return (doc, exit code)."""
    code = 0
    try:
        wb.cmd_config_set(list(pairs), dry_run=True, as_json=True)
    except SystemExit as e:
        code = e.code
    doc = json.loads(capsys.readouterr().out)
    _validate(doc, "config-dry-run")
    return doc, code


_CHOICE_KEYS = sorted(k for k, r in wb._CONFIG_VALUE_RULES.items() if "choices" in r)
_CLAMP_KEYS = sorted(k for k, r in wb._CONFIG_VALUE_RULES.items() if r.get("clamps"))


@pytest.mark.parametrize("key", _CHOICE_KEYS)
def test_every_listed_choice_is_accepted_and_nothing_else(env, capsys, key):
    for choice in wb._CONFIG_VALUE_RULES[key]["choices"]:
        doc, code = _dry(env, capsys, f"{key}={choice}")
        assert code == 0 and doc["base"][0]["key"] == key, choice
    doc, code = _dry(env, capsys, f"{key}=bogus")
    assert code == 2 and doc["error"]["code"] == "invalid_value"


@pytest.mark.parametrize("key", _CLAMP_KEYS)
def test_clamp_bounds_are_the_ones_the_validator_uses(env, capsys, key):
    lo, hi = wb._rule_bounds(key)
    doc, code = _dry(env, capsys, f"{key}={hi * 10}")
    assert code == 0 and doc["base"][0]["new"] == hi
    assert any(w.startswith(f"{key} clamped") for w in doc["warnings"])
    doc, _ = _dry(env, capsys, f"{key}={hi}")
    assert doc["base"][0]["new"] == hi and doc["warnings"] == []


@pytest.mark.parametrize("key", sorted(k for k, r in wb._CONFIG_VALUE_RULES.items()
                                       if r["type"] == "boolean"))
def test_boolean_keys_take_on_and_off(env, capsys, key):
    assert _dry(env, capsys, f"{key}=on")[0]["base"][0]["new"] is True
    assert _dry(env, capsys, f"{key}=off")[0]["base"][0]["new"] is False


# ── --config-set --dry-run ──
def _files():
    out = {}
    for name in sorted(os.listdir(".")):
        if os.path.isdir(name):
            continue
        st = os.stat(name)
        with open(name, "rb") as f:
            out[name] = (st.st_mtime_ns, f.read())
    return out


def test_dry_run_reports_the_diff_and_writes_and_signals_nothing(env, capsys):
    before = _files()
    doc, code = _dry(env, capsys, "maxloss=0.02", "recdelay=99999", "multiplier=3",
                     "proxy=http://u9:p9secret@10.0.0.9:3128", "lanes=4", "lane_split=none",
                     "lane2.multiplier=default", "lane3.sdloss=2:3", "piscope=run")
    assert code == 0
    assert _files() == before and env["kills"] == []
    base = {e["key"]: e for e in doc["base"]}
    assert base["lanes"] == {"key": "lanes", "old": 3, "new": 4, "apply": "restart"}
    assert base["lane_split"]["new"] == "none" and base["lane_split"]["apply"] == "restart"
    assert base["max_loss"] == {"key": "max_loss", "old": 0.005, "new": 0.02, "apply": "live"}
    assert base["recurring_delay"]["new"] == 86400.0
    assert base["multiplier"]["apply"] == "restart"
    assert base["bet_value"]["new"] == wb._derive_bet_value(3.0, "over")
    assert base["profit_increment_scope"]["new"] == "run"
    assert base["proxy"] == {"key": "proxy", "old": "http://10.0.0.1:8080",
                             "new": "http://10.0.0.9:3128", "apply": "live"}
    assert doc["lanes"] == {
        "2": [{"key": "multiplier", "old": 3.0, "new": None, "apply": "restart", "removed": True}],
        "3": [{"key": "streak_delay_loss", "old": None, "new": [2, 3.0], "apply": "live",
               "removed": False}]}
    assert doc["warnings"] == ["recurring_delay clamped from 99999 to 86400 (allowed: 0..86400)"]


def test_dry_run_never_prints_a_secret(env, capsys):
    wb.cmd_config_set(["proxy=http://u9:p9secret@10.0.0.9:3128",
                       "events_url=redis://x:pw9secret@127.0.0.1:6380/2"], dry_run=True, as_json=True)
    out = capsys.readouterr().out
    for secret in (*SECRETS, "p9secret", "pw9secret", "u9"):
        assert secret not in out


_BAD_INPUTS = [
    ("invalid_value", ["maxloss=abc"]),
    ("invalid_value", ["recdelay=nan"]),
    ("invalid_argument", ["foo=1"]),
    ("invalid_argument", ["maxloss"]),
    ("invalid_argument", ["lanes=99"]),
    ("invalid_lane_override", ["lane2.proxy=http://10.0.0.9:80"]),
    ("api_key_refused", ["api_key=zzz"]),
]


@pytest.mark.parametrize("code_name,pairs", _BAD_INPUTS)
def test_invalid_dry_run_matches_the_real_set(env, capsys, code_name, pairs):
    doc, code = _dry(env, capsys, *pairs)
    assert code == 2 and doc["error"]["code"] == code_name
    before = _files()
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_set(list(pairs))
    assert e.value.code == 2
    printed = " ".join(env["buf"].getvalue().split())     # Rich wraps long lines
    assert printed.startswith(" ".join(doc["error"]["message"].split()))
    assert _files() == before and env["kills"] == []


def test_dry_run_without_config_exits_1(env, capsys):
    os.remove("config.json")
    doc, code = _dry(env, capsys, "maxloss=0.01")
    assert code == 1 and doc["error"]["code"] == "no_config"


def test_rich_dry_run(env, capsys):
    wb.cmd_config_set(["maxloss=0.02", "lane2.multiplier=default"], dry_run=True)
    out = env["buf"].getvalue()
    assert "Dry run: nothing saved" in out
    assert "max_loss: 0.00500000  →  0.02000000  (live)" in out
    assert "lane2.multiplier: 3  →  base  (restart)" in out
    assert capsys.readouterr().out == "" and env["kills"] == []


# ── the real set validates everything before it writes ──
@pytest.mark.parametrize("pairs", [
    ["lane2.multiplier=3", "maxloss=abc"],
    ["lanes=4", "lane3.maxwins=5", "foo=1"],
    ["lane_split=both", "lane2.proxy=http://10.0.0.9:80"],
])
def test_failed_pair_writes_nothing_and_signals_no_lane(env, pairs):
    before = _files()
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_set(pairs)
    assert e.value.code == 2
    assert _files() == before and env["kills"] == []
    assert "Saved to" not in env["buf"].getvalue()


# ── the real CLI entry point ──
def _cli(*argv):
    import subprocess
    run_env = {**os.environ, "APP_ENV": "testing", "HOME": os.getcwd(),
               "WOLFBET_CONFIG_PATH": os.path.abspath("config.json"),
               "WOLFBET_PID_PATH": os.path.abspath("none.pid"),
               "WOLFBET_STATE_PATH": os.path.abspath("live.json")}
    run_env.pop("WOLFBET_API_KEY", None)
    return subprocess.run([sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"), *argv],
                          capture_output=True, text=True, env=run_env, timeout=60)


def test_cli_schema_and_dry_run(env):
    r = _cli("--config-schema")
    assert r.returncode == 0, r.stderr
    _validate(json.loads(r.stdout), "config-schema")
    assert json.loads(_cli("--config-schema", "--json").stdout) == json.loads(r.stdout)
    before = _files()
    r = _cli("--dry-run", "--json", "--config-set", "maxloss=0.01")
    assert r.returncode == 0, r.stderr
    _validate(json.loads(r.stdout), "config-dry-run")
    r = _cli("--dry-run", "--json", "--config-set", "maxloss=abc")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "invalid_value"
    assert _files() == before


def test_cli_flag_combinations(env):
    r = _cli("--stats", "--dry-run")
    assert r.returncode == 2 and "--dry-run works with --config-set only" in r.stderr
    r = _cli("--json", "--config-set", "maxloss=0.01")
    assert r.returncode == 2 and r.stdout == ""
