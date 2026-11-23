"""Events settings: config-set validation, config file round trip, live reload, --status row and doctor check.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11e_events_config.py -q
"""

import io
import json
import os
import re
import sys
import types

import pytest
from rich.console import Console

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from core.events import EventPublisher  # noqa: E402

_PASSWORD = "pw-" + "only-in-tests"
_URL = f"redis://publisher:{_PASSWORD}@10.0.0.5:6379/0?ssl_cert_reqs=none"


class _FakePipe:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def xadd(self, stream, fields, maxlen=None, approximate=False):
        self._ops.append((stream, fields["data"]))

    def execute(self):
        self._client.entries.extend(self._ops)


class _FakeRedis:
    def __init__(self):
        self.entries = []

    def pipeline(self, transaction=True):
        return _FakePipe(self)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Temp config/state/DB paths, a captured console, state restored after."""
    for attr in ("DB_PATH", "STATE_PATH", "CONFIG_PATH", "RUN_PATH", "PID_PATH"):
        monkeypatch.setattr(wb, attr, str(tmp_path / attr.lower()))
    monkeypatch.delenv("WOLFBET_EVENTS_URL", raising=False)
    monkeypatch.delenv("WOLFBET_API_KEY", raising=False)
    monkeypatch.setattr(wb, "_event_publisher", None)
    monkeypatch.setattr(wb._http_session, "proxies", {})
    out = io.StringIO()
    monkeypatch.setattr(wb, "console", Console(file=out, width=200, color_system=None))
    built = []

    def factory(url, lane=1):
        client = _FakeRedis()
        built.append((url, client))
        return EventPublisher(url, lane=lane, client=client)
    monkeypatch.setattr(wb, "EventPublisher", factory)
    s = wb.state
    saved = {k: v for k, v in vars(s).items() if k != "lock"}
    s.events_enabled, s.events_url = False, None
    wb.init_db()
    s.session_id = wb._db_start_session()
    yield types.SimpleNamespace(out=out, built=built)
    wb._stop_events()
    for key in [k for k in vars(s) if k != "lock" and k not in saved]:
        delattr(s, key)
    for key, value in saved.items():
        setattr(s, key, value)


def _write_config(**data):
    with open(wb.CONFIG_PATH, "w") as f:
        json.dump(data, f)


def test_keys_are_registered_everywhere():
    for key in ("events_enabled", "events_url"):
        assert key in wb._CONFIG_KEYS
        assert key in wb._TWEAK_PARAMS
        assert key in wb._LANE_FORBIDDEN_KEYS
        assert key in wb._LIVE_RELOAD_KEYS
        assert any(f == key for f, _ in wb._CONFIG_SHOW_FIELDS)


@pytest.mark.parametrize("value, expected", [("on", True), ("true", True), ("1", True),
                                             ("off", False), ("false", False), ("0", False)])
def test_events_enabled_accepts_on_off(cfg, value, expected):
    wb._apply_mutation({"events_enabled": value})
    assert wb.state.events_enabled is expected


def test_events_enabled_rejects_other_values(cfg):
    with pytest.raises(ValueError, match="on or off"):
        wb._apply_mutation({"events_enabled": "maybe"})


@pytest.mark.parametrize("url", ["redis://127.0.0.1:6379/0", "rediss://cache.example:6380",
                                 "unix:///run/redis/redis.sock", _URL])
def test_events_url_accepts_redis_schemes(cfg, url):
    msgs = wb._apply_mutation({"events_url": url})
    assert wb.state.events_url == url
    assert _PASSWORD not in " ".join(msgs)


@pytest.mark.parametrize("url", ["http://10.0.0.5:6379", "redis://", "redis://:pw@:99999",
                                 f"ftp://u:{_PASSWORD}@h"])
def test_events_url_rejects_other_urls_without_echoing_them(cfg, url):
    with pytest.raises(ValueError) as exc:
        wb._apply_mutation({"events_url": url})
    assert _PASSWORD not in str(exc.value)
    assert "redis://host" in str(exc.value)


def test_events_url_off_clears_it(cfg):
    wb.state.events_url = _URL
    wb._apply_mutation({"events_url": "off"})
    assert wb.state.events_url is None


def test_redacted_url_hides_credentials_and_query():
    assert wb._redact_url(_URL) == "redis://10.0.0.5:6379"
    assert wb._redact_url("unix:///run/redis.sock") == "unix:///run/redis.sock"
    assert wb._redact_url(None) == "off"
    assert wb._fmt_config_value("events_url", None) == "[dim]off[/]"
    assert _PASSWORD not in wb._fmt_config_value("events_url", _URL)


def test_config_round_trip_and_env_override(cfg, monkeypatch):
    wb.state.events_enabled, wb.state.events_url = True, _URL
    wb.save_config()
    wb.state.events_enabled, wb.state.events_url = False, None
    assert wb.load_config()
    assert wb.state.events_enabled is True and wb.state.events_url == _URL

    monkeypatch.setenv("WOLFBET_EVENTS_URL", "redis://override:6379")
    assert wb.load_config()
    assert wb.state.events_url == "redis://override:6379"


def test_config_set_bad_url_exits_2_and_keeps_the_file(cfg):
    _write_config(currency="usdt", events_enabled=False, events_url=None)
    before = open(wb.CONFIG_PATH).read()
    with pytest.raises(SystemExit) as exc:
        wb.cmd_config_set(["events_url=http://10.0.0.5:6379"])
    assert exc.value.code == 2
    assert open(wb.CONFIG_PATH).read() == before
    assert "Mutation failed" in cfg.out.getvalue()


def test_config_show_redacts_events_url(cfg):
    _write_config(currency="usdt", events_enabled=True, events_url=_URL)
    wb.cmd_config_show()
    shown = cfg.out.getvalue()
    assert "redis://10.0.0.5:6379" in shown
    assert _PASSWORD not in shown


def test_reload_turns_events_on_switches_url_and_turns_them_off(cfg):
    _write_config(events_enabled=True, events_url=_URL)
    wb._sig_reload(None, None)
    first = wb._event_publisher
    assert first is not None and cfg.built[-1][0] == _URL

    _write_config(events_enabled=True, events_url="redis://10.0.0.6:6379")
    wb._sig_reload(None, None)
    assert wb._event_publisher is not first
    assert cfg.built[-1][0] == "redis://10.0.0.6:6379"

    _write_config(events_enabled=False, events_url="redis://10.0.0.6:6379")
    wb._sig_reload(None, None)
    assert wb._event_publisher is None


def test_reload_event_shows_the_events_url_redacted(cfg):
    _write_config(events_enabled=True, events_url=_URL)
    wb._sig_reload(None, None)
    client = cfg.built[-1][1]
    wb._stop_events()

    events = [json.loads(d) for _, d in client.entries]
    reloads = [e["payload"]["data"] for e in events
               if e["type"] == "lifecycle" and e["payload"]["kind"] == "reload"]
    assert reloads == [{"applied": {"events_enabled": [False, True],
                                    "events_url": ["off", "redis://10.0.0.5:6379"]}}]
    assert all(_PASSWORD not in json.dumps(e) for e in events)


def test_status_shows_events_row(cfg):
    wb._save_state_file()
    wb.cmd_status()
    assert re.search(r"Events\s+off", cfg.out.getvalue())

    cfg.out.truncate(0)
    cfg.out.seek(0)
    wb.state.events_enabled, wb.state.events_url = True, _URL
    wb._start_events()
    wb._save_state_file()
    wb.cmd_status()
    text = cfg.out.getvalue()
    assert re.search(r"Events\s+on\s+· sent \d+\s+· dropped \d+\s+· connected", text)
    assert _PASSWORD not in text and "10.0.0.5" not in text


def test_snapshot_events_dict(cfg):
    assert wb._state_snapshot()["events"] == {"enabled": False, "running": False}
    wb.state.events_enabled, wb.state.events_url = True, _URL
    wb._start_events()
    ev = wb._state_snapshot()["events"]
    assert ev["enabled"] and ev["running"]
    assert set(ev) >= {"queued", "sent", "dropped", "overflow", "connected", "last_error"}


def _fake_redis_module(ping):
    mod = types.ModuleType("redis")

    class Redis:
        @staticmethod
        def from_url(url, socket_connect_timeout=None, socket_timeout=None):
            assert socket_connect_timeout == 1 and socket_timeout == 1
            return types.SimpleNamespace(ping=ping)
    mod.Redis = Redis
    return mod


def test_events_check_off(cfg):
    _write_config(events_enabled=False)
    wb.cmd_events_check()
    assert "Events: off" in cfg.out.getvalue()


def test_events_check_ok(cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(lambda: True))
    _write_config(events_enabled=True, events_url=_URL)
    wb.cmd_events_check()
    text = cfg.out.getvalue()
    assert "Redis OK (redis://10.0.0.5:6379)" in text and _PASSWORD not in text


def test_events_check_unreachable(cfg, monkeypatch):
    def ping():
        raise ConnectionRefusedError(f"cannot reach {_URL}")
    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(ping))
    _write_config(events_enabled=True, events_url=_URL)
    wb.cmd_events_check()
    text = cfg.out.getvalue()
    assert "Redis unreachable at redis://10.0.0.5:6379 (ConnectionRefusedError)" in text
    assert _PASSWORD not in text


def test_events_check_without_redis_package(cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", None)
    _write_config(events_enabled=True, events_url=_URL)
    wb.cmd_events_check()
    assert "redis package not installed" in cfg.out.getvalue()


def test_wolfctl_doctor_runs_the_events_check():
    with open(os.path.join(_WOLFBET_DIR, "wolfctl")) as f:
        src = f.read()
    doctor = src[src.index("cmd_doctor() {"):src.index("─── Prescriptions ───")]
    assert '"$PYTHON" "$BOT" --events-check' in doctor
    assert re.search(r"^WOLFCTL_VERSION=3\.23\b", src, re.MULTILINE)
