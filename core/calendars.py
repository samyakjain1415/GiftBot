"""Read birthdays out of Google Calendar and Outlook.

Both providers are OAuth 2.0 authorization-code flows differing only in URLs,
scopes and response shape, so they share one code path. Neither needs an SDK —
httpx is already a dependency.

Birthdays live in a contacts-backed calendar as recurring all-day events titled
things like "Ashna's birthday", so the name has to be recovered from the summary.
"""

import logging
import os
import re
from datetime import date, timedelta
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

PROVIDERS = {
    "google": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/calendar.readonly",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "extra_auth": {"access_type": "offline", "prompt": "consent"},
    },
    "outlook": {
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access Calendars.Read",
        "client_id_env": "MS_CLIENT_ID",
        "client_secret_env": "MS_CLIENT_SECRET",
        "extra_auth": {},
    },
}

# Occasions worth buying a gift for, and how their events tend to be titled.
OCCASIONS = ("birthday", "anniversary")

_PATTERNS = [
    # "Ashna's birthday", "Priya's 10th anniversary"
    r"^(.*?)'s\s+(?:\d+(?:st|nd|rd|th)\s+)?{kw}\b",
    # "Birthday: Ravi Kumar"
    r"^{kw}\s*[:\-–]\s*(.+)$",
    # "Meera — Birthday"
    r"^(.*?)\s*[-–—]\s*{kw}\b",
    # "Nikhil Birthday"
    r"^(.+?)\s+{kw}\b",
]


def configured(provider: str) -> bool:
    spec = PROVIDERS.get(provider)
    return bool(spec and os.getenv(spec["client_id_env"]) and os.getenv(spec["client_secret_env"]))


def _redirect_uri() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/oauth/callback"


def auth_url(provider: str, state: str) -> str:
    """Where to send the user to grant calendar access."""
    spec = PROVIDERS[provider]
    params = {
        "client_id": os.environ[spec["client_id_env"]],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": spec["scope"],
        "state": state,
        **spec["extra_auth"],
    }
    return spec["auth"] + "?" + httpx.QueryParams(params).__str__()


async def exchange_code(provider: str, code: str) -> str:
    """Authorization code -> access token."""
    spec = PROVIDERS[provider]
    data = {
        "code": code,
        "client_id": os.environ[spec["client_id_env"]],
        "client_secret": os.environ[spec["client_secret_env"]],
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(spec["token"], data=data)
        r.raise_for_status()
        return r.json()["access_token"]


def extract_event(summary: str) -> tuple[str, str] | None:
    """Recover (name, occasion) from an event title, or None if not gift-worthy."""
    text = (summary or "").strip()
    lowered = text.lower()
    for occasion in OCCASIONS:
        if occasion not in lowered:
            continue
        for template in _PATTERNS:
            match = re.match(template.format(kw=occasion), text, re.I)
            if not match:
                continue
            name = match.group(1).strip(" '\"-–—:")
            if name and name.lower() not in OCCASIONS:
                return name[:60], occasion
    return None


def extract_name(summary: str) -> str | None:
    """Backwards-compatible helper: just the name."""
    found = extract_event(summary)
    return found[0] if found else None


def _iso_date(value: str | None) -> str | None:
    """All-day events carry a date; timed events an ISO datetime."""
    if not value:
        return None
    return value[:10] if len(value) >= 10 else None


def is_personal_calendar(calendar_id: str) -> bool:
    """Exclude subscribed public calendars, which are full of false positives.

    National and religious holiday calendars carry entries like "Hazrat Ali's
    Birthday" or "Guru Nanak Jayanti" that parse exactly like a friend's
    birthday. Contact birthdays live on addressbook#contacts, which must stay.
    """
    return "#holiday@" not in calendar_id and "#sports@" not in calendar_id


async def list_calendars(token: str, limit: int = 30) -> list[dict]:
    """Personal calendars the user can read.

    Contact birthdays do not live on `primary` — Google puts them on a separate
    auto-generated calendar (addressbook#contacts@group.v.calendar.google.com),
    so reading only `primary` finds nothing.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://www.googleapis.com/calendar/v3/users/me/calendarList",
            headers={"Authorization": f"Bearer {token}"},
            params={"maxResults": str(limit)},
        )
        r.raise_for_status()
        items = r.json().get("items", [])

    kept, skipped = [], []
    for c in items:
        if not c.get("id"):
            continue
        entry = {"id": c["id"], "name": c.get("summary", c["id"])}
        (kept if is_personal_calendar(c["id"]) else skipped).append(entry)
    if skipped:
        logger.info("skipping public calendars: %s", ", ".join(s["name"] for s in skipped))
    return kept


async def fetch_birthdays(provider: str, token: str, months: int = 13) -> list[dict]:
    """Gift-worthy events in the next year, as [{name, date, occasion}].

    Looks a year ahead so each occasion appears exactly once regardless of where
    in the year the user connects.
    """
    start = date.today()
    end = start + timedelta(days=31 * months)
    headers = {"Authorization": f"Bearer {token}"}
    found: dict[str, dict] = {}
    scanned = 0

    if provider == "google":
        try:
            calendars_ = await list_calendars(token)
        except httpx.HTTPError:
            logger.warning("could not list calendars; falling back to primary")
            calendars_ = [{"id": "primary", "name": "primary"}]

        async with httpx.AsyncClient(timeout=45) as client:
            for cal in calendars_:
                try:
                    r = await client.get(
                        f"https://www.googleapis.com/calendar/v3/calendars/"
                        f"{quote(cal['id'], safe='')}/events",
                        headers=headers,
                        params={
                            "timeMin": f"{start.isoformat()}T00:00:00Z",
                            "timeMax": f"{end.isoformat()}T00:00:00Z",
                            "singleEvents": "true",
                            "maxResults": "2500",
                        },
                    )
                    r.raise_for_status()
                    items = r.json().get("items", [])
                except httpx.HTTPError:
                    logger.info("skipping unreadable calendar %s", cal["name"])
                    continue

                scanned += len(items)
                hits = 0
                for item in items:
                    parsed = extract_event(item.get("summary", ""))
                    if not parsed:
                        continue
                    name, occasion = parsed
                    when = _iso_date(
                        (item.get("start") or {}).get("date")
                        or (item.get("start") or {}).get("dateTime")
                    )
                    if when and name not in found:
                        found[name] = {"name": name, "date": when, "occasion": occasion}
                        hits += 1
                logger.info("calendar %-32s %4d events, %d matched", cal["name"][:32],
                            len(items), hits)
    else:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.get(
                "https://graph.microsoft.com/v1.0/me/calendar/calendarView",
                headers=headers,
                params={
                    "startDateTime": f"{start.isoformat()}T00:00:00",
                    "endDateTime": f"{end.isoformat()}T00:00:00",
                    "$top": "999",
                },
            )
            r.raise_for_status()
            items = r.json().get("value", [])
        scanned = len(items)
        for item in items:
            parsed = extract_event(item.get("subject", ""))
            if not parsed:
                continue
            name, occasion = parsed
            when = _iso_date((item.get("start") or {}).get("dateTime"))
            if when and name not in found:
                found[name] = {"name": name, "date": when, "occasion": occasion}

    logger.info("%s: %d occasion(s) from %d events", provider, len(found), scanned)
    return [found[k] for k in sorted(found)]
