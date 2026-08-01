"""Linq/iMessage entrypoint — separate process from the Telegram bot.

    python main_linq.py            # run the webhook server
    python main_linq.py subscribe https://<ngrok>.ngrok.io/webhook

Both entrypoints share core/ and the same database; only the adapter differs.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from adapters.linq import client, webhook
from core.db import init_db

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "subscribe":
        result = asyncio.run(client.subscribe(sys.argv[2]))
        secret = result.get("signing_secret", "")
        print("subscription id :", result.get("id"))
        print("signing secret  :", secret)
        print("\nSave it now — Linq cannot show it again:")
        print(f"LINQ_WEBHOOK_SECRET={secret}")
        return

    init_db()
    webhook.serve(int(os.getenv("LINQ_PORT", "8080")))


if __name__ == "__main__":
    main()
