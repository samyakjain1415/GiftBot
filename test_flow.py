"""Self-check for the Prava payment flow.

Stubs the Prava API and drives the state machine end to end, so a broken
transition or a dropped report-status call fails here instead of in Telegram.
Run: python test_flow.py
"""

import asyncio
import tempfile
from pathlib import Path

from core import agent, db

calls: dict = {}


async def fake_create_session(**kwargs):
    calls["create"] = kwargs
    return {
        "session_id": "ses_test123",
        "iframe_url": "https://checkout.prava.space/s/abc?session=ses_test123",
    }


async def fake_report_status(session_id, txn_ref_id, txn_status="APPROVED"):
    calls["report"] = (session_id, txn_ref_id, txn_status)
    return {"status": "completed"}


def ready_result():
    return {
        "session_id": "ses_test123",
        "status": "awaiting_result",
        "transactions": [
            {
                "txn_id": "txn_1",
                "status": "awaiting_result",
                "line_items": [
                    {"txn_ref_id": "tli_001", "token": "4323126882557932", "dynamic_cvv": "957"}
                ],
            }
        ],
    }


async def drive_to_gift_choice(user="u1"):
    """/start -> /test -> context -> gift keyboard shown."""
    ev = lambda t: agent.NormalizedEvent(user_id=user, platform="test", text=t)
    assert "Welcome" in (await agent.handle(ev("/start"))).text
    assert "birthday" in (await agent.handle(ev("/test"))).text
    reply = await agent.handle(ev("she loves books and tea"))
    assert reply.keyboard == agent.GIFTS, reply.keyboard
    return ev


async def test_happy_path():
    ev = await drive_to_gift_choice("u1")

    reply = await agent.handle(ev("gift:reading:25"))
    assert reply.poll_session_id == "ses_test123"
    assert reply.checkout_url.startswith("https://"), reply.checkout_url
    assert calls["create"]["amount"] == "25.00", calls["create"]
    assert calls["create"]["merchant"]["country_code_iso2"] == "US"
    assert agent._session("u1")["state"] == "AWAITING_PAYMENT"

    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, ready_result())
    reply = await agent.await_payment("u1")

    assert "sandbox payment completed" in reply.text, reply.text
    assert "tli_001" in reply.text
    # Without report-status the Prava session never reaches "completed".
    assert calls["report"] == ("ses_test123", "tli_001", "APPROVED"), calls.get("report")
    assert agent._session("u1")["state"] == "CONFIRMED"


async def test_timeout_returns_to_gift_picker():
    ev = await drive_to_gift_choice("u2")
    await agent.handle(ev("gift:spa:70"))

    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, None)
    reply = await agent.await_payment("u2")

    assert "timed out" in reply.text, reply.text
    assert reply.keyboard == agent.GIFTS
    assert agent._session("u2")["state"] == "SHOWING_GIFTS"


async def test_failed_payment_does_not_record_order():
    ev = await drive_to_gift_choice("u3")
    await agent.handle(ev("gift:gourmet:45"))
    calls.pop("report", None)

    failed = {"status": "failed", "transactions": []}
    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, failed)
    reply = await agent.await_payment("u3")

    assert "did not complete" in reply.text, reply.text
    assert "report" not in calls, "must not settle a failed payment"
    assert agent._session("u3")["state"] == "SHOWING_GIFTS"


async def main():
    db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
    db.init_db()
    agent.prava.create_session = fake_create_session
    agent.prava.report_status = fake_report_status

    await test_happy_path()
    await test_timeout_returns_to_gift_picker()
    await test_failed_payment_does_not_record_order()
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
