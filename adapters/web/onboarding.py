"""Onboarding web adapter — serves the setup page and accepts its payload.

The page runs in a browser that has no idea who the visitor is on Telegram, so
it parks its payload against a one-time token and hands the user a deep link.
The bot redeems the token on /start, which is also what proves the messaging
connection actually happened.
"""

import json
import logging
import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from core import calendars
from core.db import attach_cap, create_setup, merge_friends, setup_status

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

    # An empty setup is legitimate: calendar sync needs somewhere to put the
    # birthdays it finds, and that happens before any are entered by hand.
    friends = _clean_friends(data.get("friends"))

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
    # "?&" not "&": RFC 5724 wants the query to start with "?" (Android drops
    # the body otherwise, or folds it into the recipient), and iOS accepts both.
    return f"sms:{number}?&body=" + quote(f"/start {token}")


def calendar_start(provider: str, token: str) -> tuple[int, str]:
    """Where to send the browser to begin calendar consent. (status, location)."""
    if provider not in calendars.PROVIDERS:
        return 404, ""
    if not calendars.configured(provider):
        return 503, ""
    if setup_status(token) is None:
        return 404, ""
    # state carries which setup and which provider through the round trip.
    return 302, calendars.auth_url(provider, f"{provider}:{token}")


async def calendar_callback(code: str, state: str) -> tuple[int, str]:
    """Finish consent: read birthdays and store them. (status, html)."""
    provider, _, token = state.partition(":")
    if provider not in calendars.PROVIDERS or not token:
        return 400, _result_page("Something went wrong", "That link was malformed.")
    try:
        access = await calendars.exchange_code(provider, code)
        found = await calendars.fetch_birthdays(provider, access)
    except Exception:
        logger.exception("calendar sync failed for %s", provider)
        return 502, _result_page(
            "Couldn't read your calendar",
            "The connection failed. You can close this and add birthdays by hand.",
        )

    added = merge_friends(token, found)
    logger.info("%s sync stored %d of %d birthdays", provider, added, len(found))
    if not found:
        return 200, _result_page(
            "No birthdays found",
            "We couldn't see any birthday events in that calendar. "
            "Close this tab and add them by hand.",
        )
    return 200, _result_page(
        f"Added {added} birthday{'s' if added != 1 else ''}",
        "Head back to finish setting up.",
        [f["name"] for f in found][:12],
    )


def _result_page(title: str, message: str, names: list[str] | None = None) -> str:
    items = ""
    if names:
        rows = "".join(f"<li>{escape(n)}</li>" for n in names)
        items = f"<ul>{rows}</ul>"
    return f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GiftBot</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0a14;
 color:#ece9f5;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px}}
 .c{{max-width:340px;text-align:center}} h1{{font-size:20px;margin:14px 0 8px}}
 p{{color:#9b93b4;font-size:14px;margin:0}} .i{{font-size:44px}}
 ul{{list-style:none;padding:0;margin:18px 0 0;text-align:left}}
 li{{background:#1e1830;border:1px solid #2c2340;border-radius:10px;
 padding:9px 12px;margin-bottom:6px;font-size:14px}}
 a{{display:block;margin-top:22px;background:linear-gradient(135deg,#a855f7,#7c3aed);
 color:#fff;text-decoration:none;font-weight:600;padding:13px;border-radius:11px}}
</style>
<div class="c"><div class="i">🎂</div><h1>{escape(title)}</h1>
<p>{escape(message)}</p>{items}<a href="/">Back to setup</a></div>"""


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
