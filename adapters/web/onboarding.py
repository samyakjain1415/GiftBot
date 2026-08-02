"""Onboarding web adapter — serves the setup page and accepts its payload.

The page runs in a browser that has no idea who the visitor is on Telegram, so
it parks its payload against a one-time token and hands the user a deep link.
The bot redeems the token on /start, which is also what proves the messaging
connection actually happened.
"""

import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from core.db import attach_cap, create_setup, setup_status

logger = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent.parent.parent / "web" / "index.html"


def _bot_username() -> str:
    return os.getenv("TELEGRAM_BOT_USERNAME", "GiftingPalBot")


def _linq_number() -> str:
    # Read on use, not at import: load_dotenv() runs after this module is
    # imported, so a module-level read sees an empty environment.
    return os.getenv("LINQ_PHONE_NUMBER", "")

MAX_BODY = 64 * 1024      # an onboarding payload is tiny; refuse anything odd
MAX_FRIENDS = 50
MAX_NAME = 60


def page_html() -> bytes:
    return PAGE.read_bytes()


def _clean_friends(raw) -> list[dict]:
    """Keep only well-formed entries — this payload comes from the open web."""
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_FRIENDS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()[:MAX_NAME]
        date = str(item.get("date", "")).strip()
        # ISO date, as produced by <input type="date">
        if not name or len(date) != 10 or date[4] != "-" or date[7] != "-":
            continue
        try:
            int(date[:4]), int(date[5:7]), int(date[8:10])
        except ValueError:
            continue
        out.append({"name": name, "date": date})
    return out


def create_setup_token(body: bytes) -> tuple[int, dict]:
    """POST /api/setup -> {token, deep_link}. Returns (status, response)."""
    if len(body) > MAX_BODY:
        return 413, {"error": "payload too large"}
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return 400, {"error": "invalid json"}

    friends = _clean_friends(data.get("friends"))
    if not friends:
        return 400, {"error": "at least one friend with a name and date is required"}

    cap = data.get("cap")
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        cap = None
    if cap is not None and not (0 < cap <= 10000):
        cap = None

    token = create_setup({"friends": friends, "cap": cap})
    logger.info("created setup token for %d friend(s)", len(friends))
    return 200, {
        "token": token,
        "telegram_link": f"https://t.me/{_bot_username()}?start={token}",
        # iOS opens Messages with the recipient and body prefilled, so the user
        # taps send rather than typing a token by hand.
        "imessage_link": _sms_link(token),
        "imessage_available": bool(_linq_number()),
    }


def _sms_link(token: str) -> str | None:
    number = _linq_number()
    if not number:
        return None
    return f"sms:{number}&body=" + quote(f"/start {token}")


def check_status(token: str) -> tuple[int, dict]:
    """GET /api/setup/<token> -> {claimed}. The page polls this to confirm the
    messaging connection really happened rather than trusting a checkbox."""
    status = setup_status(token)
    if status is None:
        return 404, {"error": "unknown token"}
    return 200, status


def set_cap(token: str, body: bytes) -> tuple[int, dict]:
    """POST /api/setup/<token>/cap -> {ok}."""
    try:
        data = json.loads(body or b"{}")
        cap = float(data.get("cap"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 400, {"error": "invalid cap"}
    if not (0 < cap <= 10000):
        return 400, {"error": "cap out of range"}
    if not attach_cap(token, cap):
        return 404, {"error": "unknown token"}
    return 200, {"ok": True}
