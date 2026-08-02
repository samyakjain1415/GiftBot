"""Gift matching — Gemini picks from the curated catalog given friend context + budget.

Uses the Gemini REST API directly (httpx is already a dependency); the
responseSchema field gives us structured JSON without the google-genai SDK.
"""

import json
import os
import re
from pathlib import Path

import httpx

from core import search

MODEL = "gemini-3.6-flash"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

CATALOG: list[dict] = json.loads(
    (Path(__file__).resolve().parent.parent / "gifts.json").read_text(encoding="utf-8")
)["items"]
BY_ID = {item["id"]: item for item in CATALOG}

_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "blurb": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "blurb", "reason"],
            },
        }
    },
    "required": ["picks"],
}

# The model never sees a product description — only a title, price and merchant.
# Anything richer than the title would be invented, and the user is about to
# spend money against this text.
_ACCURACY_RULE = (
    "For 'blurb', describe only what the product title already states, in one "
    "short sentence. Never invent materials, quantities, colours, flavours, "
    "sizes or features that are not in the title. If the title is vague, keep "
    "the blurb vague."
)


def parse_budget(text: str) -> float | None:
    """Pull a number out of '$50', 'around 50 dollars', 'under 40'."""
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


def _affordable(budget: float | None) -> list[dict]:
    if budget is None:
        return CATALOG
    within = [item for item in CATALOG if float(item["price"]) <= budget]
    # Never return an empty menu — show the cheapest options instead of nothing.
    return within or sorted(CATALOG, key=lambda i: float(i["price"]))[:6]


def _describe(item: dict) -> str:
    line = f"- {item['id']} | {item['name']} | ${item['price']} | {item['merchant']}"
    return line + (f" | {', '.join(item['tags'])}" if item.get("tags") else "")


def _prompt(context: str, budget: float | None, pool: list[dict], n: int) -> str:
    listing = "\n".join(_describe(i) for i in pool)
    budget_line = f"Budget: ${budget:.0f} or less." if budget else "No stated budget."
    return (
        f"You are picking birthday gifts for someone's friend.\n\n"
        f"What we know about them: {context}\n"
        f"{budget_line}\n\n"
        f"Choose the {n} best gifts from this catalog. Use only these ids:\n{listing}\n\n"
        f"{_ACCURACY_RULE}\n\n"
        f"For 'reason', give at most 12 words connecting the gift to what we know "
        f"about them. Be specific, not generic. Use they/them unless the notes "
        f"make the person's pronouns clear."
    )


async def suggest(context: str, budget: float | None, n: int = 3) -> list[dict]:
    """Return n products, each with a 'reason'.

    Live Google Shopping results when available, curated catalog otherwise, and
    cheapest-in-budget if Gemini itself fails. Each layer degrades to the next.
    """
    pool = await search.find_products(context, budget) or _affordable(budget)
    by_id = {item["id"]: item for item in pool}
    fallback = sorted(pool, key=lambda i: float(i["price"]))[:n]

    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return [{**item, "reason": "budget pick"} for item in fallback]

    payload = {
        "contents": [{"parts": [{"text": _prompt(context, budget, pool, n)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
            "temperature": 0.7,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ENDPOINT, params={"key": key}, json=payload)
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        picks = json.loads(raw)["picks"]
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        return [{**item, "reason": "budget pick"} for item in fallback]

    chosen = [
        {**by_id[p["id"]], "reason": p.get("reason", ""), "blurb": p.get("blurb", "")}
        for p in picks
        if p.get("id") in by_id
    ]
    return chosen[:n] or [{**item, "reason": "budget pick"} for item in fallback]
