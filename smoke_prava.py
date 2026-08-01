"""Live Prava sandbox smoke test — the real 4-step checkout, no Telegram involved.

    python smoke_prava.py

Creates a real session, prints the checkout link, waits for you to pay with a
sandbox test card (OTP 456789) + passkey, then settles via report-status and
prints the final session status. Proves the integration end to end.
"""

import asyncio
import os

from dotenv import load_dotenv

from core import prava
from core.agent import MERCHANT

load_dotenv()


async def main():
    if not os.getenv("PRAVA_API_KEY"):
        raise SystemExit("PRAVA_API_KEY missing from .env — add your sk_test_... key first.")

    session = await prava.create_session(
        user_id="smoke_test_user",
        user_email="smoke@example.com",
        amount="25.00",
        description="Cozy Reading Kit",
        merchant=MERCHANT,
    )
    session_id = session["session_id"]
    print(f"session_id : {session_id}")
    print(f"expires_at : {session.get('expires_at')}")
    print(f"\n  OPEN THIS AND PAY:\n  {session['iframe_url']}\n")
    print("  test card + OTP 456789 + Face ID / Touch ID")
    print("\npolling for approval (Ctrl-C to abort)...")

    result = await prava.poll_until_ready(session_id)
    if result is None:
        raise SystemExit("timed out waiting for approval")

    print(f"status after approval: {result['status']}")
    line_item = prava.first_credentialed_line_item(result)
    if line_item is None:
        raise SystemExit(f"no credentials issued — result: {result}")

    print(f"txn_ref_id : {line_item['txn_ref_id']}")
    print(f"token      : ...{str(line_item['token'])[-4:]} (one-time, merchant-locked)")

    await prava.report_status(session_id, line_item["txn_ref_id"], "APPROVED")

    final = await prava.get_payment_result(session_id)
    print(f"\nfinal session status: {final['status']}")
    print("PASS" if final["status"] == "completed" else "unexpected final status")


if __name__ == "__main__":
    asyncio.run(main())
