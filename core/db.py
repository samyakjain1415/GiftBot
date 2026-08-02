import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
            -- Onboarding handoff: the web page has no idea who the user is on
            -- Telegram, so it parks its payload against a one-time token that
            -- the bot redeems via /start <token>.
            CREATE TABLE IF NOT EXISTS setups (
                token       TEXT    PRIMARY KEY,
                payload     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                claimed_by  INTEGER REFERENCES users(id)
            );
        """)
        # Added after the original schema; SQLite has no ADD COLUMN IF NOT EXISTS.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "spend_cap" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN spend_cap REAL")


def upsert_user(external_id: str, platform: str = "telegram") -> int:
    """Return the user id for this external id, creating the row if needed.

    external_id is the Telegram user id or the Linq phone number. The platform
    matters: the scheduler has to know which adapter can reach this person.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, platform) VALUES (?, ?)",
            (external_id, platform),
        )
        # Existing rows predate the platform argument and all claim 'telegram'.
        conn.execute(
            "UPDATE users SET platform = ? WHERE telegram_id = ? AND platform != ?",
            (platform, external_id, platform),
        )
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (external_id,)
        ).fetchone()
        return row["id"]


def next_birthday(birthday: str, today: date | None = None) -> date | None:
    """Next occurrence of a stored birthday (year may be a birth year)."""
    today = today or date.today()
    try:
        parsed = date.fromisoformat(birthday)
    except (ValueError, TypeError):
        return None
    try:
        this_year = parsed.replace(year=today.year)
    except ValueError:
        return None  # 29 Feb in a non-leap year
    if this_year >= today:
        return this_year
    try:
        return parsed.replace(year=today.year + 1)
    except ValueError:
        return None


def schedule_reminder(
    friend_id: int, birthday: str, lead_days: int = 6, from_date: date | None = None
) -> str | None:
    """Queue one pending reminder ahead of a friend's next birthday.

    Replaces any existing pending row for that friend so an edited birthday
    doesn't leave a stale reminder behind.

    from_date moves the reference point forward. After firing a reminder the
    caller must pass a date past that birthday, otherwise next_birthday returns
    the same occurrence and the reminder becomes due again on the next tick —
    which resends forever.
    """
    upcoming = next_birthday(birthday, from_date)
    if upcoming is None:
        return None
    fire = (upcoming - timedelta(days=lead_days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE friend_id = ? AND status = 'pending'", (friend_id,)
        )
        conn.execute(
            "INSERT INTO reminders (friend_id, fire_date, status) VALUES (?, ?, 'pending')",
            (friend_id, fire),
        )
    return fire


def due_reminders(platform: str, today: str | None = None) -> list[dict]:
    """Pending reminders that have come due, for users on this platform."""
    today = today or date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.id, r.fire_date, f.id AS friend_id, f.name, f.birthday,"
            "       u.telegram_id AS external_id"
            "  FROM reminders r"
            "  JOIN friends f ON f.id = r.friend_id"
            "  JOIN users   u ON u.id = f.user_id"
            " WHERE r.status = 'pending' AND r.fire_date <= ? AND u.platform = ?",
            (today, platform),
        ).fetchall()
        return [dict(r) for r in rows]


def resume_reminder(user_id: int, within_days: int = 3) -> dict | None:
    """The most recently fired reminder for this user, if it is still recent.

    Conversation state lives in memory, so a restart between sending a reminder
    and the user replying would otherwise strand them on "Type /start". This
    lets the reply be recognised anyway.
    """
    cutoff = (date.today() - timedelta(days=within_days)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.id, f.id AS friend_id, f.name, f.birthday"
            "  FROM reminders r JOIN friends f ON f.id = r.friend_id"
            " WHERE f.user_id = ? AND r.status = 'sent' AND r.fire_date >= ?"
            " ORDER BY r.fire_date DESC, r.id DESC LIMIT 1",
            (user_id, cutoff),
        ).fetchone()
        return dict(row) if row else None


def claim_reminder(reminder_id: int) -> bool:
    """Mark a reminder sent. False if another worker already took it."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE reminders SET status = 'sent' WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        return cur.rowcount == 1


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


def create_setup(payload: dict) -> str:
    """Park an onboarding payload against a one-time token. Returns the token."""
    token = secrets.token_urlsafe(16)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO setups (token, payload, created_at) VALUES (?, ?, ?)",
            (token, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
    return token


def claim_setup(token: str, user_id: int) -> dict | None:
    """Redeem a setup token for a user, creating their friends.

    Returns the payload, or None if the token is unknown or already claimed —
    a token must never be usable twice.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM setups WHERE token = ? AND claimed_by IS NULL", (token,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE setups SET claimed_by = ? WHERE token = ?", (user_id, token))
        payload = json.loads(row["payload"])
        if payload.get("cap"):
            conn.execute(
                "UPDATE users SET spend_cap = ? WHERE id = ?", (payload["cap"], user_id)
            )

        scheduled = []
        for friend in payload.get("friends", []):
            name, birthday = friend.get("name"), friend.get("date")
            if not (name and birthday):
                continue
            existing = conn.execute(
                "SELECT id FROM friends WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE friends SET birthday = ? WHERE id = ?", (birthday, existing["id"])
                )
                friend_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO friends (user_id, name, birthday) VALUES (?, ?, ?)",
                    (user_id, name, birthday),
                )
                friend_id = cur.lastrowid
            scheduled.append((friend_id, birthday))

    # Outside the transaction above: schedule_reminder opens its own connection.
    for friend_id, birthday in scheduled:
        schedule_reminder(friend_id, birthday)
    return payload


def merge_friends(token: str, friends: list[dict]) -> int:
    """Add friends to a setup, whether or not it has been claimed yet.

    Calendar sync can happen before or after the user connects a messaging
    channel, so the destination depends on the setup's state: an unclaimed
    setup takes them into its payload, a claimed one straight onto the user.
    Returns how many were stored.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, claimed_by FROM setups WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return 0
        claimed_by = row["claimed_by"]

        if claimed_by is None:
            payload = json.loads(row["payload"])
            existing = {f["name"] for f in payload.get("friends", [])}
            fresh = [f for f in friends if f["name"] not in existing]
            payload.setdefault("friends", []).extend(fresh)
            conn.execute(
                "UPDATE setups SET payload = ? WHERE token = ?",
                (json.dumps(payload), token),
            )
            return len(fresh)

        stored = []
        for friend in friends:
            name, birthday = friend.get("name"), friend.get("date")
            if not (name and birthday):
                continue
            found = conn.execute(
                "SELECT id FROM friends WHERE user_id = ? AND name = ?", (claimed_by, name)
            ).fetchone()
            if found:
                conn.execute(
                    "UPDATE friends SET birthday = ? WHERE id = ?", (birthday, found["id"])
                )
                stored.append((found["id"], birthday))
            else:
                cur = conn.execute(
                    "INSERT INTO friends (user_id, name, birthday) VALUES (?, ?, ?)",
                    (claimed_by, name, birthday),
                )
                stored.append((cur.lastrowid, birthday))

    for friend_id, birthday in stored:
        schedule_reminder(friend_id, birthday)
    return len(stored)


def setup_status(token: str) -> dict | None:
    """Whether a setup token has been redeemed yet — lets the page confirm the
    messaging connection instead of asking the user to self-certify it."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, claimed_by FROM setups WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        claimed_by = row["claimed_by"]
        if claimed_by is None:
            names = [f.get("name") for f in json.loads(row["payload"]).get("friends", [])]
        else:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM friends WHERE user_id = ? ORDER BY name", (claimed_by,))]
        return {"claimed": claimed_by is not None, "friends": [n for n in names if n]}


def attach_cap(token: str, cap: float) -> bool:
    """Record a spending cap for a setup, before or after it is claimed.

    The cap is chosen a step after the messaging connection, by which point the
    token may already be redeemed, so it lands on the user rather than the
    pending payload.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, claimed_by FROM setups WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if row["claimed_by"] is not None:
            conn.execute(
                "UPDATE users SET spend_cap = ? WHERE id = ?", (cap, row["claimed_by"])
            )
        else:
            payload = json.loads(row["payload"])
            payload["cap"] = cap
            conn.execute(
                "UPDATE setups SET payload = ? WHERE token = ?", (json.dumps(payload), token)
            )
        return True


def get_spend_cap(user_id: int) -> float | None:
    with get_conn() as conn:
        row = conn.execute("SELECT spend_cap FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["spend_cap"] if row and row["spend_cap"] else None


def list_friends(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, birthday, context, last_gift FROM friends"
            " WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_order(friend_id: int, gift: str, amount: float, prava_txn: str | None = None) -> None:
    """Record a settled order. prava_txn is the transaction evidence — persist it."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO orders (friend_id, gift, amount, prava_txn, status)"
            " VALUES (?, ?, ?, ?, 'confirmed')",
            (friend_id, gift, amount, prava_txn),
        )
        conn.execute("UPDATE friends SET last_gift = ? WHERE id = ?", (gift, friend_id))
