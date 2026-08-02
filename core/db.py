import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path("giftbot.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT    UNIQUE NOT NULL,
                platform    TEXT    NOT NULL DEFAULT 'telegram',
                prava_token TEXT
            );
            CREATE TABLE IF NOT EXISTS friends (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL REFERENCES users(id),
                name      TEXT    NOT NULL,
                birthday  TEXT,
                context   TEXT,
                last_gift TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                friend_id   INTEGER NOT NULL REFERENCES friends(id),
                fire_date   TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                friend_id   INTEGER NOT NULL REFERENCES friends(id),
                gift        TEXT,
                amount      REAL,
                prava_txn   TEXT,
                status      TEXT    NOT NULL DEFAULT 'pending'
            );
        """)


def upsert_user(telegram_id: str) -> int:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, platform) VALUES (?, 'telegram')",
            (telegram_id,),
        )
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row["id"]


def seed_ashna(user_id: int) -> int:
    """Reuse this user's Ashna if present.

    INSERT OR IGNORE cannot dedupe here — there is no UNIQUE constraint on
    (user_id, name) — so every /test used to create another duplicate.
    """
    birthday = (date.today() + timedelta(days=6)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM friends WHERE user_id = ? AND name = 'Ashna' ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO friends (user_id, name, birthday) VALUES (?, 'Ashna', ?)",
            (user_id, birthday),
        )
        return cur.lastrowid


def save_context(friend_id: int, context: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE friends SET context = ? WHERE id = ?", (context, friend_id))


def create_order(friend_id: int, gift: str, amount: float, prava_txn: str | None = None) -> None:
    """Record a settled order. prava_txn is the transaction evidence — persist it."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO orders (friend_id, gift, amount, prava_txn, status)"
            " VALUES (?, ?, ?, ?, 'confirmed')",
            (friend_id, gift, amount, prava_txn),
        )
        conn.execute("UPDATE friends SET last_gift = ? WHERE id = ?", (gift, friend_id))
