"""HTTP entrypoint — serves the onboarding page and receives Linq webhooks.

    python server.py                          # run the server
    python server.py subscribe https://.../webhook

One process on one port, because ngrok's free tier gives a single tunnel and
both the page and the webhook have to live behind it.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from adapters.linq import client, webhook
from adapters.web import onboarding
from core import scheduler
from core.agent import begin_reminder
from core.db import init_db

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class Router(BaseHTTPRequestHandler):
    server_version = "GiftBot"

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self):  # noqa: N802 — stdlib naming
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, onboarding.page_html(), "text/html; charset=utf-8")
            except OSError:
                logger.exception("could not read onboarding page")
                self._send(500, b"page unavailable")
        elif path == "/health":
            self._json(200, {"ok": True})
        else:
            self._send(404, b"not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if path == "/webhook":
            try:
                self._send(webhook.handle_delivery(self.headers, raw))
            except Exception:
                logger.exception("webhook handling failed")
                self._send(500)
        elif path == "/api/setup":
            status, payload = onboarding.create_setup_token(raw)
            self._json(status, payload)
        else:
            self._send(404, b"not found")

    def log_message(self, *args):
        pass  # stdlib logs every request to stderr; we log what matters ourselves


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "subscribe":
        result = asyncio.run(client.subscribe(sys.argv[2]))
        print("subscription id :", result.get("id"))
        print("signing secret  :", result.get("signing_secret"))
        print("\nSave it now — Linq cannot show it again:")
        print(f"LINQ_WEBHOOK_SECRET={result.get('signing_secret')}")
        return

    init_db()

    if os.getenv("LINQ_API_KEY") and os.getenv("LINQ_PHONE_NUMBER"):
        threading.Thread(target=_run_linq_scheduler, daemon=True).start()
    else:
        logger.info("Linq not configured — reminder scheduler for iMessage disabled")

    port = int(os.getenv("PORT", os.getenv("LINQ_PORT", "8080")))
    logger.info("GiftBot server on port %s  (GET / • POST /api/setup • POST /webhook)", port)
    ThreadingHTTPServer(("0.0.0.0", port), Router).serve_forever()


def _run_linq_scheduler() -> None:
    """Reminder loop for iMessage users, in this process so it shares session state."""
    async def send(phone: str, text: str) -> None:
        await client.send_text(phone, text)

    asyncio.run(scheduler.run("linq", send, begin_reminder))


if __name__ == "__main__":
    main()
