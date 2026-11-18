"""Bot event publisher: ships bet, state and lifecycle events to Redis Streams.

The betting thread only calls the emit_* methods, which build one tuple and
append it to a bounded deque. A daemon thread drains the deque every 100 ms
and sends each batch as one pipelined XADD. Redis being slow, down or missing
never reaches the betting thread: failed batches are dropped and counted, and
when the queue is full new events are dropped and counted, without building
them (see reserve). Every event carries a per-process seq, and a dropped
event still uses up its seq, so a consumer sees any loss as a gap.

Queued events are flat frames of plain values (kind, seq, ts_ms, fields...)
rather than tuples: a bet then leaves no garbage-collected object alive on the
queue, so a busy queue never makes the collector pause the betting thread.
The sender also gives up the interpreter lock every few events while it
encodes, so a betting thread never waits behind a whole batch.

Wire format (schema v1): each stream entry has one field, ``data``, holding
compact JSON ``{"v", "svc", "lane", "pid", "seq", "ts_ms", "type", "payload"}``.
"""

import collections
import itertools
import json
import logging
import os
import threading
import time

SCHEMA_VERSION = 1

BET_FIELDS = (
    "session_id", "amount", "multiplier", "rule", "bet_value", "result_value",
    "state", "profit", "balance_after", "session_profit", "total_bets",
    "current_streak", "api_ms",
)

LIFECYCLE_KINDS = (
    "start", "stop", "pause", "resume", "reload", "cycle",
    "session_open", "session_close", "error",
)

_STREAMS = {"bet": "bets", "state": "state", "lifecycle": "lifecycle"}
_FRAME_WIDTH = {"bet": 3 + len(BET_FIELDS), "state": 4, "lifecycle": 5}
_DEFAULT_MAXLEN = {"bets": 200_000, "state": 20_000, "lifecycle": 20_000}

_TICK_S = 0.1
_BATCH_MAX = 500
_YIELD_EVERY = 32
_BACKOFF_MIN_S = 0.5
_BACKOFF_MAX_S = 30.0
_LOG_EVERY_S = 60.0
_CONNECT_TIMEOUT_S = 0.1
_REPLY_TIMEOUT_S = 1.0

logger = logging.getLogger("wolfbet.events")


class EventPublisher:
    """Bounded, non-blocking event queue with its own Redis sender thread.

    ``client`` replaces the Redis client built from ``url``; tests pass a fake.
    """

    def __init__(self, url, *, lane=1, stream_prefix="wolfbet", maxlen=None,
                 queue_max=50_000, client=None):
        self.url = url
        self.lane = lane
        self.svc = stream_prefix
        self.pid = os.getpid()
        self._prefix = stream_prefix
        self._maxlen = dict(_DEFAULT_MAXLEN, **(maxlen or {}))
        self._queue = collections.deque()
        self._queue_max = queue_max * _FRAME_WIDTH["bet"]
        self._seq = itertools.count()
        self._client = client
        self._enabled = False
        self._stop = threading.Event()
        self._thread = None
        self._backoff = 0.0
        self._last_log = 0.0
        self._queued = 0
        self._overflow = 0
        self._sent = 0
        self._dropped = 0
        self._connected = False
        self._last_error = ""

    def reserve(self):
        """True when the next event fits in the queue. When full, count it as lost and return False.

        Lets the betting thread skip building an event that would be dropped anyway.
        """
        if not self._enabled:
            return False
        if len(self._queue) < self._queue_max:
            return True
        next(self._seq)
        self._overflow += 1
        return False

    def emit_bet(self, fields):
        """Queue one bet; ``fields`` follows BET_FIELDS order."""
        if len(fields) != len(BET_FIELDS):
            if self._enabled:
                next(self._seq)
                self._dropped += 1
            return
        self._emit("bet", fields)

    def emit_state(self, state):
        """Queue one state snapshot dict."""
        self._emit("state", (state,))

    def emit_lifecycle(self, kind, data):
        """Queue one lifecycle event; ``kind`` is one of LIFECYCLE_KINDS."""
        self._emit("lifecycle", (kind, data))

    def _emit(self, kind, values):
        if not self._enabled:
            return
        try:
            seq = next(self._seq)
            if len(self._queue) >= self._queue_max:
                self._overflow += 1
                return
            self._queue.extend((kind, seq, time.time_ns() // 1_000_000) + values)
            self._queued += 1
        except Exception:
            pass

    def _pop_frame(self):
        """Take one queued event off the front as (kind, seq, ts_ms, data)."""
        q = self._queue
        kind, seq, ts_ms = q.popleft(), q.popleft(), q.popleft()
        if kind == "bet":
            data = tuple(q.popleft() for _ in BET_FIELDS)
        elif kind == "lifecycle":
            data = (q.popleft(), q.popleft())
        else:
            data = q.popleft()
        return kind, seq, ts_ms, data

    def start(self):
        """Start the sender thread. Returns False when Redis cannot be used."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._client is None:
            try:
                import redis
            except ImportError:
                logger.warning("events disabled: the redis package is not installed")
                return False
            self._client = redis.Redis.from_url(
                self.url,
                socket_connect_timeout=_CONNECT_TIMEOUT_S,
                socket_timeout=_REPLY_TIMEOUT_S,
            )
        self._enabled = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="EventPublisher")
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        """Stop emitting, flush what is queued if Redis answers, and join."""
        self._enabled = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def stats(self):
        """Counters for status output."""
        return {
            "queued": self._queued,
            "sent": self._sent,
            "dropped": self._dropped,
            "overflow": self._overflow,
            "last_error": self._last_error,
            "connected": self._connected,
        }

    def encode(self, item):
        """Return (stream key, JSON text) for one queued item."""
        kind, seq, ts_ms, data = item
        if kind == "bet":
            if len(data) != len(BET_FIELDS):
                raise ValueError("bet tuple does not match BET_FIELDS")
            payload = dict(zip(BET_FIELDS, data))
        elif kind == "lifecycle":
            payload = {"kind": data[0], "data": data[1]}
        else:
            payload = data
        event = {
            "v": SCHEMA_VERSION, "svc": self.svc, "lane": self.lane, "pid": self.pid,
            "seq": seq, "ts_ms": ts_ms, "type": kind, "payload": payload,
        }
        text = json.dumps(event, separators=(",", ":"), allow_nan=False)
        return f"{self._prefix}:{_STREAMS[kind]}", text

    def _run(self):
        while not self._stop.wait(self._backoff or _TICK_S):
            while self._queue and not self._stop.is_set():
                if not self._send_batch():
                    break
        while self._queue and not self._backoff:
            if not self._send_batch():
                break

    def _send_batch(self):
        batch = []
        while self._queue and len(batch) < _BATCH_MAX:
            batch.append(self._pop_frame())
        bad = 0
        try:
            pipe = self._client.pipeline(transaction=False)
            for n, item in enumerate(batch, 1):
                if n % _YIELD_EVERY == 0:
                    time.sleep(0)
                try:
                    stream, text = self.encode(item)
                except (ValueError, TypeError):
                    bad += 1
                    continue
                pipe.xadd(stream, {"data": text},
                          maxlen=self._maxlen[stream.rsplit(":", 1)[1]], approximate=True)
            if bad < len(batch):
                pipe.execute()
        except Exception as exc:
            # After a reply timeout Redis may already hold the batch, so "dropped"
            # means unconfirmed; consumers detect real loss from seq gaps.
            self._dropped += len(batch)
            self._connected = False
            self._last_error = type(exc).__name__
            self._backoff = min(max(self._backoff * 2, _BACKOFF_MIN_S), _BACKOFF_MAX_S)
            now = time.monotonic()
            if now - self._last_log >= _LOG_EVERY_S:
                self._last_log = now
                logger.warning("events: send failed (%s), %d dropped so far, retry in %.1fs",
                               self._last_error, self._dropped, self._backoff)
            return False
        self._dropped += bad
        self._sent += len(batch) - bad
        self._connected = True
        self._backoff = 0.0
        return True
