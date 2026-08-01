"""Linq webhook receiver — verifies signatures, drives core, replies via Linq.

Runs a stdlib HTTP server (no new dependency). Linq expects a fast 200, so the
request is acknowledged immediately and the conversation runs on a worker
thread; replies go out through the send API rather than the HTTP response.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from adapters.linq import client
from core.agent import BotResponse, NormalizedEvent, await_payment, handle
from core.prava import PravaUnavailable

logger = logging.getLogger(__name__)

MAX_SKEW_SECONDS = 300

# Last options offered per phone number, so a reply of "2" maps back to the
# callback_data core expects. iMessage has no buttons to carry it for us.
_last_options: dict[str, list[str]] = {}

OUTAGE = (
    "⚠️ Prava's sandbox isn't responding right now. That's on their side — "
    "nothing was charged. Try again shortly."
)


def verify_signature(headers, raw_body: bytes, secret: str) -> bool:
    """HMAC-SHA256 over '{webhook-id}.{webhook-timestamp}.{rawBody}', base64."""
    webhook_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signature = headers.get("webhook-signature")
    if not (webhook_id and timestamp and signature):
        return False

    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
            return False  # replay protection
    except ValueError:
        return False

    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False

    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    # Header holds space-separated "v1,<base64>" entries.
    for entry in signature.split():
        _, _, candidate = entry.partition(",")
        if hmac.compare_digest(candidate, expected):
            return True
    return False


def render(response: BotResponse, phone: str) -> str:
    """Flatten a BotResponse into text — no buttons exist on iMessage."""
    text = response.text
    if response.keyboard:
        _last_options[phone] = [data for _, data in response.keyboard]
        options = "\n".join(
            f"{i}. {label}" for i, (label, _) in enumerate(response.keyboard, 1)
        )
        text += f"\n\n{options}\n\nReply with a number (1-{len(response.keyboard)})."
    if response.checkout_url:
        text += f"\n\n{response.checkout_url}"
    return text


def resolve(phone: str, text: str) -> str:
    """Turn a numeric reply back into the callback_data core is expecting."""
    options = _last_options.get(phone)
    if options and text.strip().isdigit():
        index = int(text.strip()) - 1
        if 0 <= index < len(options):
            return options[index]
    return text


async def _converse(phone: str, text: str) -> None:
    event = NormalizedEvent(user_id=phone, platform="linq", text=resolve(phone, text))
    try:
        response = await handle(event)
    except PravaUnavailable:
        await client.send_text(phone, OUTAGE)
        return

    await client.send_text(phone, render(response, phone))

    if response.poll_session_id:
        try:
            settled = await await_payment(phone)
            await client.send_text(phone, render(settled, phone))
        except PravaUnavailable:
            await client.send_text(phone, OUTAGE)


def _process(payload: dict) -> None:
    if payload.get("event_type") != "message.received":
        return
    data = payload.get("data") or {}
    phone = data.get("from")
    text = " ".join(
        part.get("value", "")
        for part in (data.get("parts") or [])
        if part.get("type") == "text"
    ).strip()
    if not (phone and text):
        return
    try:
        asyncio.run(_converse(phone, text))
    except Exception:
        logger.exception("linq conversation failed for %s", phone)


class Handler(BaseHTTPRequestHandler):
    secret = ""

    def do_POST(self):  # noqa: N802 — stdlib naming
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        if not verify_signature(self.headers, raw, self.secret):
            logger.warning("rejected webhook with bad signature")
            self.send_response(401)
            self.end_headers()
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        # Acknowledge before working: Linq wants a fast 200, and the flow can
        # take minutes while the user completes a Prava checkout.
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=_process, args=(payload,), daemon=True).start()

    def log_message(self, *args):
        pass  # stdlib logs every request to stderr; we have real logging


def serve(port: int = 8080) -> None:
    Handler.secret = os.environ["LINQ_WEBHOOK_SECRET"]
    logger.info("Linq webhook listening on port %s", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
