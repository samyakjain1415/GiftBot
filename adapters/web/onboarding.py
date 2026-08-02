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

from core.db import create_setup

logger = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent.parent.parent / "web" / "index.html"
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "GiftingPalBot")

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
        "deep_link": f"https://t.me/{BOT_USERNAME}?start={token}",
    }
