"""WebSocket routes for real-time updates."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bot_db import _connect_ro, get_session
from ..websocket import manager, authenticate_ws
from ..services import system_metrics, stream_logs, SERVICES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

POLL_INTERVAL_SECONDS = 2.0
_BET_COLUMNS = (
    "id, timestamp, amount, multiplier, bet_value, rule, "
    "result_value, state, profit, balance_after"
)

_pollers: dict[str, asyncio.Task] = {}


def _read_new_bets(user_id: int, session_id: int, after_id: int) -> list[dict]:
    """Bets newer than after_id, shaped for the live chart."""
    try:
        conn = _connect_ro(user_id)
        rows = conn.execute(
            f"SELECT {_BET_COLUMNS} FROM bets "
            "WHERE session_id = ? AND id > ? ORDER BY id ASC",
            (session_id, after_id),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("Failed to read bets for user %s session %s", user_id, session_id)
        return []

    points = []
    for r in rows:
        row = dict(r)
        ts_str = row.get("timestamp") or ""
        try:
            ts_display = datetime.fromisoformat(
                str(ts_str).replace("Z", "+00:00")
            ).strftime("%H:%M:%S")
        except (ValueError, AttributeError):
            ts_display = str(ts_str)
        points.append({
            "time": row["id"],
            "value": round(row.get("balance_after") or 0.0, 8),
            "bet_num": row["id"],
            "amount": round(row.get("amount") or 0.0, 8),
            "profit": round(row.get("profit") or 0.0, 8),
            "state": row.get("state") or "loss",
            "target": round(row.get("multiplier") or 0.0, 4),
            "result": str(round(row.get("result_value") or 0.0, 4)),
            "ts": ts_display,
        })
    return points


async def _fetch_new_bets(user_id: int, session_id: int, after_id: int) -> list[dict]:
    """Keeps the blocking SQLite read off the event loop."""
    return await asyncio.to_thread(_read_new_bets, user_id, session_id, after_id)


async def _poll_channel(channel: str, user_id: int, session_id: int):
    """One poller per session channel, shared by every subscriber."""
    cancelled = False
    last_bet_id = 0
    try:
        while manager.has_subscribers(channel):
            try:
                session = await asyncio.to_thread(get_session, user_id, session_id)
                if session:
                    await manager.broadcast(channel, "session_update", session)
                for point in await _fetch_new_bets(user_id, session_id, last_bet_id):
                    last_bet_id = point["time"]
                    await manager.broadcast(channel, "new_bet", point)
            except Exception:
                logger.exception("Session poll failed for %s", channel)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _pollers.pop(channel, None)
        if not cancelled and manager.has_subscribers(channel):
            _start_poller(channel, user_id, session_id)


def _start_poller(channel: str, user_id: int, session_id: int):
    task = _pollers.get(channel)
    if task is not None and not task.done():
        return
    _pollers[channel] = asyncio.create_task(_poll_channel(channel, user_id, session_id))


def _stop_poller(channel: str):
    task = _pollers.pop(channel, None)
    if task is not None and not task.done():
        task.cancel()


@router.websocket("/ws/metrics")
async def ws_metrics(ws: WebSocket):
    """Admin-only: system metrics every 3 seconds."""
    payload = await authenticate_ws(ws)
    if not payload or payload.get("role") != "admin":
        await ws.close(code=4001, reason="Unauthorized")
        return

    await manager.connect(ws, "admin:metrics")
    try:
        while True:
            metrics = system_metrics()
            await manager.send_to(ws, "metrics", metrics)
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, "admin:metrics")


@router.websocket("/ws/logs/{service_label}")
async def ws_logs(ws: WebSocket, service_label: str):
    """Admin-only: live log streaming."""
    payload = await authenticate_ws(ws)
    if not payload or payload.get("role") != "admin":
        await ws.close(code=4001, reason="Unauthorized")
        return

    service_name = SERVICES.get(service_label)
    if not service_name:
        await ws.close(code=4002, reason="Unknown service")
        return

    await manager.connect(ws, f"admin:logs:{service_label}")
    try:
        async for line in stream_logs(service_name, lines=100):
            await manager.send_to(ws, "log", {"line": line})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, f"admin:logs:{service_label}")


@router.websocket("/ws/session/{user_id}/{session_id}")
async def ws_session(ws: WebSocket, user_id: int, session_id: int):
    """Live session updates for an admin or the owning Telegram user."""
    payload = await authenticate_ws(ws)
    if not payload:
        await ws.close(code=4001, reason="Unauthorized")
        return

    if payload.get("role") == "tg_user":
        token_uid = int(payload["sub"].split(":")[1])
        if token_uid != user_id:
            await ws.close(code=4003, reason="Forbidden")
            return

    channel = f"session:{user_id}:{session_id}"
    await manager.connect(ws, channel)
    _start_poller(channel, user_id, session_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, channel)
        if not manager.has_subscribers(channel):
            _stop_poller(channel)
