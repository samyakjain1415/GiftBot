"""Linq Partner API client — send iMessage/RCS/SMS.

Linq documents no buttons or interactive parts, so everything the bot says
becomes plain text; links are tappable in iMessage.
"""

import os

import httpx

BASE_URL = os.getenv("LINQ_BASE_URL", "https://api.linqapp.com/api/partner/v3")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['LINQ_API_KEY']}",
        "Content-Type": "application/json",
    }


async def send_text(to: str, text: str) -> dict:
    """POST /chats — send a text message to one recipient."""
    payload = {
        "from": os.environ["LINQ_PHONE_NUMBER"],
        "to": [to],
        "message": {"parts": [{"type": "text", "value": text}]},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE_URL}/chats", json=payload, headers=_headers())
        r.raise_for_status()
        return r.json()


async def subscribe(target_url: str) -> dict:
    """POST /webhook-subscriptions — register a webhook.

    The returned signing_secret cannot be retrieved later; save it immediately.
    """
    payload = {
        "target_url": target_url,
        "subscribed_events": ["message.received"],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE_URL}/webhook-subscriptions", json=payload, headers=_headers()
        )
        r.raise_for_status()
        return r.json()
