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

# Transient server-side failures worth retrying. 4xx (bad key, bad payload) is
# our fault and retrying just wastes the user's time.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class PravaUnavailable(RuntimeError):
    """Prava is failing on their side — distinct from a bad request of ours."""


def _headers() -> dict[str, str]:
    # Fail loudly: a missing key must never silently degrade to a fake payment.
    key = os.environ["PRAVA_API_KEY"]
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _request(method: str, path: str, attempts: int = 2, **kwargs) -> dict:
    """Call Prava, retrying only transient server failures.

    Their sandbox gateway has been observed returning 504 after ~60s across all
    endpoints, so fail fast and surface a clear error rather than hanging.
    """
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.request(
                    method, f"{BASE_URL}{path}", headers=_headers(), **kwargs
                )
            if r.status_code not in _RETRY_STATUS:
                r.raise_for_status()
                return r.json()
            last = f"HTTP {r.status_code}"
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = type(exc).__name__

        if attempt + 1 < attempts:
            await asyncio.sleep(1.5 * (attempt + 1))

    raise PravaUnavailable(f"Prava unreachable after {attempts} attempts ({last})")


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
    return await _request("POST", "/v1/sessions", json=payload)


async def get_payment_result(session_id: str) -> dict:
    """GET /v1/sessions/{id}/payment-result -> status + transactions[].line_items[]"""
    return await _request("GET", f"/v1/sessions/{session_id}/payment-result")


async def report_status(session_id: str, txn_ref_id: str, txn_status: str = "APPROVED") -> dict:
    """POST /v1/sessions/{id}/report-status — flips the session to completed/failed."""
    # Retried harder: losing this call leaves a paid session stuck at awaiting_result.
    return await _request(
        "POST",
        f"/v1/sessions/{session_id}/report-status",
        attempts=4,
        json={"txn_ref_id": txn_ref_id, "txn_status": txn_status},
    )


async def poll_until_ready(
    session_id: str, interval: float = 2.0, timeout: float = 180.0
) -> dict | None:
    """Poll until credentials are issued. Returns None on timeout.

    ponytail: fixed 2s/180s cadence — docs recommend none. Swap for webhooks
    once Prava ships delivery.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = await get_payment_result(session_id)
        except PravaUnavailable:
            # A blip mid-checkout must not abandon a payment the user is completing.
            result = None
        if result and result.get("status") in _TERMINAL:
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
