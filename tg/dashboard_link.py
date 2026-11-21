"""The `/web` sign-in link for the dashboard: one use, five minutes, one Telegram user.

The token is `base64url(payload).base64url(mac)`: compact JSON with sorted keys
`{"exp", "id", "uid", "v"}`, and HMAC-SHA256 over the first segment with the secret the
dashboard also holds (`contracts/fixtures/tg_login_v1/` pins the bytes). The link puts the
token in the URL fragment, so it never reaches a server log or a Referer header.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

VERSION = 1
TTL_S = 300
MIN_SECRET_BYTES = 32
MIN_DISTINCT_CHARS = 8
_PLACEHOLDER = re.compile(r"(change.?me|replace.?me|your[-_ ]|<)"
                          r"|(secret|password|example|test|default|dummy|x+)$", re.IGNORECASE)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def secret_problem(secret: str) -> str | None:
    """Why ``secret`` cannot sign links, or None when it can."""
    if len(secret.encode()) < MIN_SECRET_BYTES:
        return f"is shorter than {MIN_SECRET_BYTES} bytes"
    if len(set(secret)) < MIN_DISTINCT_CHARS:
        return "has too few distinct characters"
    if _PLACEHOLDER.search(secret):
        return "is a placeholder"
    return None


def sign(secret: str, uid: int, link_id: str, exp: int) -> str:
    """The token for one link."""
    payload = json.dumps({"exp": exp, "id": link_id, "uid": uid, "v": VERSION},
                         sort_keys=True, separators=(",", ":"))
    body = _b64(payload.encode())
    mac = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def login_url(base_url: str, secret: str, uid: int, now: float | None = None) -> str:
    """A fresh link for ``uid``, valid for ``TTL_S`` seconds from ``now``."""
    exp = int(now if now is not None else time.time()) + TTL_S
    token = sign(secret, uid, secrets.token_urlsafe(16), exp)
    return f"{base_url.rstrip('/')}/tg/login#token={token}"
