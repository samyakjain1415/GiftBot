"""Prava Payments REST client — sandbox agentic checkout.

Flow (docs.prava.space): create session -> user pays on hosted page with passkey
-> poll payment-result for one-time card credentials -> report-status to complete.

Prava has no inbound webhooks yet ("configuration exists today, delivery is
rolling out"), so confirmation is by polling.
"""

import asyncio
import os
import time

import httpx

BASE_URL = os.getenv("PRAVA_BASE_URL", "https://sandbox.api.prava.space")

# Statuses that mean "stop polling" — credentials issued, or terminal outcome.
_TERMINAL = {"awaiting_result", "completed", "failed"}


def _headers() -> dict[str, str]:
    # Fail loudly: a missing key must never silently degrade to a fake payment.
    key = os.environ["PRAVA_API_KEY"]
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def create_session(
    user_id: str,
    user_email: str,
    amount: str,
    description: str,
    merchant: dict,
    currency: str = "USD",
) -> dict:
    """POST /v1/sessions -> {session_id, iframe_url, order_id, expires_at, ...}

    Sessions expire 15 minutes after creation; effective_until_minutes is accepted
    but does not extend that, so expect to re-mint if the user walks away.
    """
    context = {
        "merchant_details": merchant,
        "product_details": [{"description": description, "unit_price": amount, "quantity": 1}],
    }
    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "total_amount": amount,
        "currency": currency,
        "integration_type": "full_checkout",
        "purchase_context": [context],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE_URL}/v1/sessions", json=payload, headers=_headers())
        r.raise_for_status()
        return r.json()


async def get_payment_result(session_id: str) -> dict:
    """GET /v1/sessions/{id}/payment-result -> status + transactions[].line_items[]"""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE_URL}/v1/sessions/{session_id}/payment-result", headers=_headers()
        )
        r.raise_for_status()
        return r.json()


async def report_status(session_id: str, txn_ref_id: str, txn_status: str = "APPROVED") -> dict:
    """POST /v1/sessions/{id}/report-status — flips the session to completed/failed."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE_URL}/v1/sessions/{session_id}/report-status",
            json={"txn_ref_id": txn_ref_id, "txn_status": txn_status},
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()


async def poll_until_ready(
    session_id: str, interval: float = 2.0, timeout: float = 180.0
) -> dict | None:
    """Poll until credentials are issued. Returns None on timeout.

    ponytail: fixed 2s/180s cadence — docs recommend none. Swap for webhooks
    once Prava ships delivery.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await get_payment_result(session_id)
        if result.get("status") in _TERMINAL:
            return result
        await asyncio.sleep(interval)
    return None


def first_credentialed_line_item(result: dict) -> dict | None:
    """Find the line item carrying the one-time card token."""
    for txn in result.get("transactions") or []:
        for line_item in txn.get("line_items") or []:
            if line_item.get("token"):
                return line_item
    return None
