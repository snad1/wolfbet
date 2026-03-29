"""Database schema, migrations, and connection helpers.

Single source of truth for the sessions/bets DB schema.
Both the CLI bot and Telegram bot use this.
"""

import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.core.database import (  # noqa: E402
    SESSIONS_SCHEMA,
    cleanup_old_bets, db_connect, secure_path,
)


def init_db(db_path: str):
    """Initialize the SQLite database with sessions and bets tables."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(SESSIONS_SCHEMA)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER,
            timestamp    TEXT,
            amount       REAL,
            multiplier   REAL,
            rule         TEXT,
            bet_value    REAL,
            result_value REAL,
            state        TEXT,
            profit       REAL,
            balance_after REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    conn.commit()
    conn.close()


def migrate_db(db_path: str):
    """Add new columns to existing databases (safe, idempotent)."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for col, defn in [
        ("game",            "TEXT"),
        ("highest_balance", "REAL DEFAULT 0"),
        ("lowest_balance",  "REAL DEFAULT 0"),
        ("highest_win",     "REAL DEFAULT 0"),
        ("biggest_loss",    "REAL DEFAULT 0"),
        ("bets_per_minute", "REAL DEFAULT 0"),
        ("bets_per_second", "REAL DEFAULT 0"),
        ("peak_bps",        "REAL DEFAULT 0"),
        ("low_bps",         "REAL DEFAULT 0"),
        ("peak_bpm",        "REAL DEFAULT 0"),
        ("low_bpm",         "REAL DEFAULT 0"),
        ("config_snapshot", "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
