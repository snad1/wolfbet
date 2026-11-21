"""Control socket: the dashboard's host agent starts, stops and configures one Telegram user's sessions.

It speaks the agent protocol v1, JSON lines over a unix socket, as the casino agent does:
- every connection is checked against the uid/gid allowlist with the kernel's peer
  credentials before a byte is read;
- every request line is at most MAX_REQUEST_BYTES and holds exactly the protocol fields
  with their types;
- its ``mac`` is HMAC-SHA256 with the control token over the canonical JSON of the other
  fields, its ``ts`` is inside the replay window, and its id has not been used before.

A refused request gets one generic error code; the reason is logged here only, never a
token or a setting value. Commands come from a fixed table, take typed arguments, and only
touch the sessions of the uid they name, through the same functions the Telegram commands
use. Standard library only; tests pin the wire format to the dashboard's golden fixtures.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
import sys
import time
from collections import OrderedDict, defaultdict

from . import handlers
from .config import logger

PROTO_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
REPLAY_WINDOW_MS = 30_000
REPLAY_CACHE_MAX = 100_000
IDLE_TIMEOUT_S = 5.0
MAX_CONNS_PER_PEER = 4
MIN_TOKEN_BYTES = 32
MIN_DISTINCT_CHARS = 8

_FIELDS = {"proto", "id", "cmd", "args", "ts", "mac"}
_REQUIRED = _FIELDS - {"args"}
_CMD = re.compile(r"^[a-z][a-z0-9_.]*$")
_MAC = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z_]{1,32}$")
_CLOSE_AFTER = frozenset({"bad_request", "unauthorized", "replayed", "too_large"})
_UCRED = struct.Struct("3i")
_XUCRED = struct.Struct("IIh16I")


class ControlError(Exception):
    """The control socket must not start with this configuration."""


class Refused(Exception):
    """A request gets error ``code``; ``reason`` is only logged."""

    def __init__(self, code: str, reason: str, req_id=None):
        super().__init__(reason)
        self.code, self.reason, self.req_id = code, reason, req_id


def canonical_body(fields: dict) -> bytes:
    """The bytes a request mac covers: every field except ``mac``, in one fixed encoding."""
    body = {k: v for k, v in fields.items() if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(token: bytes, fields: dict) -> str:
    return hmac.new(token, canonical_body(fields), hashlib.sha256).hexdigest()


def response_line(req_id, ok: bool, data=None, code=None) -> bytes:
    """One reply line, byte for byte as the agent writes it."""
    reply = {"id": req_id, "ok": ok}
    if data is not None:
        reply["data"] = data
    if code is not None:
        reply["error"] = {"code": code}
    return json.dumps(reply, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def read_token(path: str) -> bytes:
    """The control token, created once (0600) when missing; refused when others can read it or it is weak."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    except OSError:
        raise ControlError(f"control token file {path} cannot be created") from None
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
    try:
        mode = os.stat(path).st_mode
        with open(path) as f:
            text = f.read().strip()
    except OSError:
        raise ControlError(f"control token file {path} cannot be read") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ControlError(f"control token file {path} must be readable by its owner only (chmod 600)")
    if len(text.encode()) < MIN_TOKEN_BYTES or len(set(text)) < MIN_DISTINCT_CHARS:
        raise ControlError("control token is too short or too simple")
    return text.encode()


def peer_credentials(sock: socket.socket) -> tuple:
    """(uid, gid) of the connected peer. Raises OSError when the platform cannot tell."""
    if sys.platform.startswith("linux"):
        _pid, uid, gid = _UCRED.unpack(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))
        return uid, gid
    if sys.platform == "darwin":
        _version, uid, ngroups, *groups = _XUCRED.unpack(
            sock.getsockopt(0, socket.LOCAL_PEERCRED, _XUCRED.size))
        if ngroups < 1:
            raise OSError("peer has no group")
        return uid, groups[0]
    raise OSError(f"peer credentials are not supported on {sys.platform}")


def parse_uids(raw: str) -> frozenset:
    try:
        return frozenset(int(part) for part in raw.replace(",", " ").split())
    except ValueError:
        raise ControlError("WOLFBET_TG_CONTROL_ALLOW_UIDS must be numeric uids") from None


# ── commands ─────────────────────────────────────────────
def _int(args: dict, name: str, required: bool):
    value = args.get(name)
    if value is None and not required:
        return None
    if type(value) is not int or value < 1:
        raise Refused("bad_request", f"{name} must be a positive integer")
    return value


def _str(args: dict, name: str, pattern=None, max_len: int = 256) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise Refused("bad_request", f"{name} must be a non-empty string up to {max_len} chars")
    if pattern is not None and not pattern.match(value):
        raise Refused("bad_request", f"{name} has characters that are not allowed")
    return value


def _target(uid: int, slot):
    """The (slot, engine) a command acts on, among ``uid``'s running sessions only."""
    running = handlers._get_running(uid)
    if not running:
        raise Refused("not_found", "no session running")
    if slot is not None:
        if slot not in running:
            raise Refused("not_found", f"no running session in slot {slot}")
        return slot, running[slot]
    if len(running) > 1:
        raise Refused("bad_request", "several sessions running: name a slot")
    return next(iter(running.items()))


class ControlServer:
    """Serve the control commands on ``socket_path`` for the agent; ``clock`` returns epoch seconds."""

    ARGS = {
        "start": {"uid"},
        "stop": {"uid", "slot"},
        "pause": {"uid", "slot"},
        "resume": {"uid", "slot"},
        "set": {"uid", "key", "value"},
        "status": {"uid"},
    }

    def __init__(self, token: bytes, socket_path: str, app, allow_uids=frozenset(),
                 allow_gids=frozenset(), clock=time.time):
        self.token = token
        self.socket_path = socket_path
        self.app = app
        self.allow_uids = frozenset(allow_uids) | {os.getuid()}
        self.allow_gids = frozenset(allow_gids)
        self.clock = clock
        self._seen = OrderedDict()
        self._conns = defaultdict(int)
        self._server = None

    async def start(self) -> None:
        path = self.socket_path
        if os.path.islink(path) or (os.path.exists(path) and not stat.S_ISSOCK(os.stat(path).st_mode)):
            raise ControlError(f"{path} exists and is not a socket; refusing to replace it")
        if os.path.exists(path):
            os.unlink(path)
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=path, limit=MAX_REQUEST_BYTES + 1)
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        logger.info("Control socket listening on %s", path)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    async def _handle(self, reader, writer) -> None:
        try:
            uid, gid = peer_credentials(writer.get_extra_info("socket"))
        except OSError as e:
            logger.warning("Control: refused a connection, peer unknown (%s)", type(e).__name__)
            await self._reply(writer, response_line(None, False, code="unauthorized"))
            await self._close(writer)
            return
        if uid not in self.allow_uids and gid not in self.allow_gids:
            logger.warning("Control: refused peer uid=%d gid=%d, not in the allowlist", uid, gid)
            await self._reply(writer, response_line(None, False, code="unauthorized"))
            await self._close(writer)
            return
        if self._conns[uid] >= MAX_CONNS_PER_PEER:
            await self._reply(writer, response_line(None, False, code="busy"))
            await self._close(writer)
            return
        self._conns[uid] += 1
        try:
            await self._serve(reader, writer)
        finally:
            self._conns[uid] -= 1
            await self._close(writer)

    async def _serve(self, reader, writer) -> None:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                return
            except (ValueError, asyncio.LimitOverrunError):
                logger.warning("Control: refused a request over %d bytes", MAX_REQUEST_BYTES)
                await self._reply(writer, response_line(None, False, code="too_large"))
                return
            if not line:
                return
            reply, code = await self.process(line)
            await self._reply(writer, reply)
            if code in _CLOSE_AFTER:
                return

    async def process(self, line: bytes) -> tuple:
        """Answer one request line: (reply bytes, error code or None)."""
        req_id = None
        try:
            request = self._authenticate(line)
            req_id = request["id"]
            data = await self._run(request["cmd"], request["args"])
        except Refused as e:
            req_id = e.req_id or req_id
            logger.warning("Control: refused id=%s: %s", req_id, e.reason)
            return response_line(req_id, False, code=e.code), e.code
        except Exception:
            logger.exception("Control: command failed (id=%s)", req_id)
            return response_line(req_id, False, code="internal"), "internal"
        return response_line(req_id, True, data=data), None

    def _authenticate(self, line: bytes) -> dict:
        if len(line) > MAX_REQUEST_BYTES:
            raise Refused("too_large", "request over the size cap")
        try:
            raw = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Refused("bad_request", "not JSON") from None
        if not isinstance(raw, dict):
            raise Refused("bad_request", "not an object")
        req_id = raw.get("id") if isinstance(raw.get("id"), str) else None
        req_id = req_id[:64] if req_id else None
        keys = set(raw)
        if not _REQUIRED <= keys <= _FIELDS:
            raise Refused("bad_request", "missing or unknown fields", req_id)
        args = raw.get("args", {})
        ok = (type(raw["proto"]) is int and raw["proto"] == PROTO_VERSION
              and isinstance(raw["id"], str) and 1 <= len(raw["id"]) <= 64
              and isinstance(raw["cmd"], str) and len(raw["cmd"]) <= 64 and _CMD.match(raw["cmd"])
              and isinstance(args, dict)
              and type(raw["ts"]) is int and raw["ts"] >= 0
              and isinstance(raw["mac"], str) and _MAC.match(raw["mac"]))
        if not ok:
            raise Refused("bad_request", "field types", req_id)
        fields = {"proto": raw["proto"], "id": raw["id"], "cmd": raw["cmd"], "args": args,
                  "ts": raw["ts"]}
        if not hmac.compare_digest(sign(self.token, fields), raw["mac"]):
            raise Refused("unauthorized", "bad mac", req_id)
        now_ms = int(self.clock() * 1000)
        if abs(now_ms - raw["ts"]) > REPLAY_WINDOW_MS:
            raise Refused("unauthorized", f"ts {now_ms - raw['ts']} ms off", req_id)
        self._remember(raw["id"], now_ms)
        return fields

    def _remember(self, req_id: str, now_ms: int) -> None:
        while self._seen and next(iter(self._seen.values())) <= now_ms:
            self._seen.popitem(last=False)
        if req_id in self._seen:
            raise Refused("replayed", "id already used", req_id)
        if len(self._seen) >= REPLAY_CACHE_MAX:
            raise Refused("busy", "replay cache full", req_id)
        self._seen[req_id] = now_ms + 2 * REPLAY_WINDOW_MS

    async def _run(self, cmd: str, args: dict) -> dict:
        allowed = self.ARGS.get(cmd)
        if allowed is None:
            raise Refused("unknown_command", f"unknown command {cmd!r}")
        if not set(args) <= allowed:
            raise Refused("bad_request", f"{cmd}: unknown arguments")
        uid = _int(args, "uid", required=True)
        if cmd == "status":
            running = handlers._get_running(uid)
            return {"sessions": [dict(e._state_snapshot(), slot=s) for s, e in sorted(running.items())]}
        if cmd == "set":
            key = _str(args, "key", _KEY, max_len=32)
            value = _str(args, "value")
            try:
                text, _markdown = await asyncio.to_thread(handlers.apply_setting, uid, key, value)
            except handlers.SettingError:
                raise Refused("bad_request", f"set {key}: value refused") from None
            logger.info("Control: user %d set %s", uid, key)
            return {"message": text}
        if cmd == "start":
            chat_id = handlers._engine_chat_ids.get(uid, uid)
            try:
                slot, engine, _config = await handlers.start_session(uid, chat_id, self.app)
            except handlers.StartError as e:
                raise Refused("bad_request", f"start for user {uid} failed: {e.text}") from None
            logger.info("Control: started session #%s for user %d (slot %d)", engine.session_id, uid, slot)
            return {"slot": slot, "session_id": engine.session_id}
        slot, engine = _target(uid, _int(args, "slot", required=False))
        if cmd == "stop":
            handlers.stop_slot(uid, slot, engine)
        elif cmd == "pause":
            engine.pause()
        else:
            engine.resume()
        logger.info("Control: %s user %d slot %d", cmd, uid, slot)
        return {"slots": [slot]}

    @staticmethod
    async def _reply(writer, line: bytes) -> None:
        try:
            writer.write(line)
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _close(writer) -> None:
        if writer.is_closing():
            return
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
