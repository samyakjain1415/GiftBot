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


# Marketplace sellers arrive as "Etsy - SomeShop"; the domain is the marketplace.
_KNOWN_DOMAINS = {
    "amazon": "amazon.com",
    "amazon.com": "amazon.com",
    "ebay": "ebay.com",
    "etsy": "etsy.com",
    "walmart": "walmart.com",
    "target": "target.com",
    "best buy": "bestbuy.com",
    "nordstrom": "nordstrom.com",
    "macy's": "macys.com",
    "wayfair": "wayfair.com",
}


def merchant_domain(source: str) -> str | None:
    """Best-effort merchant website from a shopping result's seller name.

    Visa refuses to mint a cryptogram when merchant_details.url is an
    aggregator link, so passing Serper's Google Shopping URL fails checkout
    with FETCH_AGENTIC_CREDS_ERROR. The domain has to plausibly belong to the
    named merchant, so derive it from the name rather than the listing link.
    """
    if not source:
        return None
    # "Etsy - ElementalBonsai" -> "Etsy"
    name = re.split(r"\s+[-–—]\s+", source.strip())[0].strip().lower()
    if not name:
        return None
    if name in _KNOWN_DOMAINS:
        return "https://www." + _KNOWN_DOMAINS[name]
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", name):
        return "https://" + name
    slug = re.sub(r"[^a-z0-9]", "", name)
    if len(slug) < 2:
        return None
    return f"https://www.{slug}.com"


def _to_item(raw: dict, index: int) -> dict | None:
    """Shape one Serper row into a catalog item.

    Note: Serper's `link` is always a Google Shopping listing URL, never the
    merchant's own domain, so merchant_url points at the verifiable offer rather
    than a guessed homepage. `source` is the real merchant name.
    """
    price = _parse_price(raw.get("price"))
    title = (raw.get("title") or "").strip()
    merchant = (raw.get("source") or "").strip()
    link = raw.get("link") or ""
    domain = merchant_domain(merchant)
    # Without a usable merchant domain the payment cannot complete, so drop the
    # product rather than offer something unbuyable.
    if not (price and title and merchant and domain):
        return None
    return {
        "id": f"live{index}_{_slug(title)}",
        "name": title[:80],
        "price": f"{price:.2f}",
        "merchant": merchant[:60],
        "merchant_url": domain,
        "listing_url": link,          # Google Shopping page, for humans not Visa
        "ucp": False,
        "live": True,
        "tags": [],
    }


async def find_products(query: str, budget: float | None, limit: int = 12) -> list[dict]:
    """Search live shopping listings. Returns [] on any failure."""
    key = os.getenv("SERPER_API_KEY")
    # Escape hatch: live results carry Google Shopping URLs as the merchant site,
    # which Visa may reject when minting agentic credentials. Setting this falls
    # back to the curated catalog, whose merchants have real domains.
    if not key or os.getenv("GIFTBOT_CATALOG_ONLY"):
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
