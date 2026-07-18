"""Shared network/config helpers used by both CLI (wolfbet.py) and TG (tg/*.py).

Extracted in v2.28.0 to end the CLI ↔ TG duplication. Pure Python — no I/O,
no threading, no engine state. Both engines wrap these with their own
transport-specific plumbing.

Includes:
- validate_proxy — accepted schemes: http / https / socks5.
- mask_proxy     — strips userinfo; safe for /config + logs.
- classify_auto_stop_reason — max_nonce / expired / "" (fixes TG's max-nonce blind spot).
- parse_streak_delay — "N:X" grammar shared by both engines.
"""

from typing import Optional, Tuple
from urllib.parse import urlparse

_PROXY_OFF_VALUES = ("", "off", "none", "direct", "0", "false", "disable", "no")


def validate_proxy(url) -> Optional[str]:
    """Return a normalized proxy URL or None.

    Accepts http://, https://, socks5://. Off-values (off/none/direct/0/…)
    return None. Raises ValueError on any other scheme.
    """
    if url is None:
        return None
    s = str(url).strip()
    if s.lower() in _PROXY_OFF_VALUES:
        return None
    if not (s.startswith("http://") or s.startswith("https://") or s.startswith("socks5://")):
        raise ValueError(
            f"Proxy must start with http://, https://, or socks5:// (got {s!r})"
        )
    return s


def mask_proxy(url) -> str:
    """Return scheme://host[:port] — strips userinfo, safe for display/logs.

    Handles the port-missing case that the TG bot's inline masker missed
    (previously printed 'None:port' or 'scheme://host:None').
    """
    if not url:
        return "direct"
    try:
        p = urlparse(url)
        if p.hostname:
            if p.port:
                return f"{p.scheme}://{p.hostname}:{p.port}"
            return f"{p.scheme}://{p.hostname}"
    except Exception:
        pass
    return "set"


def classify_auto_stop_reason(text) -> str:
    """Classify a WolfBet auto-bet error / stopReason string.

    Returns:
        "max_nonce"  — server-seed nonce exhausted → recovery requires seed rotation.
        "expired"    — UUID / session died → recovery requires fresh /auto/start.
        ""           — any other terminal reason (server-side max_profit/loss/bets).

    Callers hand this string to their engine's recovery dispatcher. Both
    CLI and TG bot use it — extracting it here fixes TG's max-nonce
    blind spot (TG previously didn't match "nonce" and never rotated).
    """
    if not text:
        return ""
    s = str(text).lower()
    if "nonce" in s:
        return "max_nonce"
    if any(k in s for k in (
        "session", "expired", "not exists", "betsession", "no query results",
    )):
        return "expired"
    return ""


def parse_streak_delay(raw) -> Tuple[int, float]:
    """Parse the "N:X" grammar used by streak_delay_* and streakbet_loss config.

    Returns (0, 0.0) on empty / malformed input; (int(N), float(X)) otherwise.
    """
    if not raw:
        return (0, 0.0)
    try:
        parts = str(raw).split(":", 1)
        return (int(parts[0]), float(parts[1]))
    except (ValueError, IndexError):
        return (0, 0.0)
