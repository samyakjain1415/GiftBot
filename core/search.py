"""Live product discovery via Serper's Google Shopping endpoint.

Returns catalog-shaped dicts so core.gifts can treat live and curated products
identically. Any failure (no key, HTTP error, unparseable price) returns [] and
the caller falls back to the curated catalog — the demo degrades, never dies.
"""

import os
import re

import httpx

ENDPOINT = "https://google.serper.dev/shopping"

# Serper prices arrive as display strings: "$24.99", "₹1,299.00", "24.99 USD".
_PRICE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    match = _PRICE.search(str(raw))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def _slug(text: str, n: int = 24) -> str:
    """Short, callback_data-safe id (Telegram caps callback_data at 64 bytes)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:n] or "item"


def _to_item(raw: dict, index: int) -> dict | None:
    price = _parse_price(raw.get("price"))
    title = (raw.get("title") or "").strip()
    merchant = (raw.get("source") or "").strip()
    link = raw.get("link") or ""
    if not (price and title and merchant and link.startswith("https://")):
        return None
    return {
        "id": f"live{index}_{_slug(title)}",
        "name": title[:80],
        "price": f"{price:.2f}",
        "merchant": merchant[:60],
        "merchant_url": link,
        "ucp": False,
        "live": True,
        "tags": [],
    }


async def find_products(query: str, budget: float | None, limit: int = 12) -> list[dict]:
    """Search live shopping listings. Returns [] on any failure."""
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return []

    payload = {"q": f"{query} gift", "num": 20}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                ENDPOINT, json=payload, headers={"X-API-KEY": key, "Content-Type": "application/json"}
            )
            r.raise_for_status()
            raw_items = r.json().get("shopping", [])
    except (httpx.HTTPError, ValueError):
        return []

    items = [it for it in (_to_item(raw, i) for i, raw in enumerate(raw_items)) if it]
    if budget is not None:
        items = [it for it in items if float(it["price"]) <= budget]
    # De-dupe by title; Google Shopping repeats the same product across merchants.
    seen, unique = set(), []
    for item in items:
        if item["name"].lower() not in seen:
            seen.add(item["name"].lower())
            unique.append(item)
    return unique[:limit]
