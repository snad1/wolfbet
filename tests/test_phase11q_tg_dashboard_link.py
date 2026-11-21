"""/web signs a one-time dashboard link the dashboard can verify; the old web link stays the fallback.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11q_tg_dashboard_link.py -q
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from tg import config as tg_config  # noqa: E402
from tg import control_socket as ctl  # noqa: E402
from tg import dashboard_link  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402

_GOLDEN = os.path.join(_HERE, "fixtures", "tg_login_v1")
SECRET = "test-dashboard-link-0123456789abcdefXYZ-q8"


def _golden():
    with open(os.path.join(_GOLDEN, "token.json")) as f:
        return json.load(f)


def test_fixture_copies_match_the_contract_checksums():
    with open(os.path.join(_GOLDEN, "SHA256SUMS")) as f:
        for line in f:
            digest, name = line.split()
            with open(os.path.join(_GOLDEN, name), "rb") as fixture:
                assert hashlib.sha256(fixture.read()).hexdigest() == digest, name


def test_the_signer_reproduces_the_golden_token():
    case = _golden()
    assert dashboard_link.sign(case["secret"], case["uid"], case["id"], case["exp"]) == case["token"]


def test_a_link_carries_the_token_in_the_fragment_and_expires_in_five_minutes():
    url = dashboard_link.login_url("https://dash.example.com/", SECRET, 42, now=1000)
    base, token = url.split("#token=")
    assert base == "https://dash.example.com/tg/login"
    body = token.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["uid"] == 42 and payload["exp"] == 1300 and payload["v"] == 1
    assert len(payload["id"]) >= 16


def test_two_links_never_share_an_id():
    one = dashboard_link.login_url("https://d", SECRET, 42)
    two = dashboard_link.login_url("https://d", SECRET, 42)
    assert one != two


@pytest.mark.parametrize("secret", ["short", "a" * 40, "change-me-please-change-me-please-0123", "x" * 64])
def test_weak_secrets_are_refused(secret):
    assert dashboard_link.secret_problem(secret) is not None


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(uid=42):
    message = _Message()
    return types.SimpleNamespace(
        message=message, effective_user=types.SimpleNamespace(id=uid, username="u")
    ), message


def test_web_sends_the_dashboard_link_when_configured(monkeypatch):
    monkeypatch.setattr(tg_config, "DASHBOARD_URL", "https://dash.example.com")
    monkeypatch.setattr(tg_config, "DASHBOARD_SECRET", SECRET)
    update, message = _update()
    asyncio.run(tg_handlers.cmd_web(update, None))
    assert "https://dash.example.com/tg/login#token=" in message.replies[0]
    assert "works once" in message.replies[0]


@pytest.mark.parametrize("url,secret", [("", SECRET), ("https://d", ""), ("https://d", "weak")])
def test_web_falls_back_to_the_old_web_when_not_configured(monkeypatch, url, secret):
    monkeypatch.setattr(tg_config, "DASHBOARD_URL", url)
    monkeypatch.setattr(tg_config, "DASHBOARD_SECRET", secret)
    monkeypatch.setattr(tg_config, "WEB_URL", "")
    monkeypatch.setattr(tg_config, "WEB_SECRET", "")
    update, message = _update()
    asyncio.run(tg_handlers.cmd_web(update, None))
    assert message.replies[0].startswith("Web dashboard not configured.")


# ── config: the saved settings, never the API key or proxy ──
NOW_MS = 1_795_200_000_000
TOKEN = b"test-control-token-0123456789abcdefXYZ"


def _ask(cmd, args):
    server = ctl.ControlServer(TOKEN, "/unused", app=None, clock=lambda: NOW_MS / 1000)
    fields = {"proto": 1, "id": f"c{args.get('uid')}", "cmd": cmd, "args": args, "ts": NOW_MS}
    fields["mac"] = ctl.sign(TOKEN, fields)
    reply, _code = asyncio.run(server.process(json.dumps(fields).encode() + b"\n"))
    return json.loads(reply)


def test_config_returns_the_users_own_settings_without_secrets(monkeypatch):
    store = {
        7: {"api_key": "k" * 40, "proxy": "http://user:pw@host:1", "currency": "btc",
            "max_loss": 0.5, "custom_rules_text": "if loss then x2", "not_a_config_key": 1},
        8: {"currency": "eth"},
    }
    monkeypatch.setattr(tg_handlers, "load_user_config", lambda uid: dict(store.get(uid, {})))
    reply = _ask("config", {"uid": 7})
    assert reply["data"] == {"config": {"currency": "btc", "max_loss": 0.5,
                                        "custom_rules_text": "if loss then x2"}}
    assert _ask("config", {"uid": 8})["data"] == {"config": {"currency": "eth"}}
    assert _ask("config", {"uid": 7, "slot": 1})["error"] == {"code": "bad_request"}
