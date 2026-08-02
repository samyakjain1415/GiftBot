"""Self-check for the full GiftBot flow.

Stubs Gemini and Prava, then drives the state machine end to end, so a broken
transition or a dropped report-status call fails here instead of in Telegram.
Run: python test_flow.py
"""

import asyncio
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from adapters.linq import webhook as linq_webhook
from adapters.web import onboarding
from core import agent, db, gifts, prava, scheduler, search

calls: dict = {}

PICKS = [
    {"id": "vahdam_tea_sampler", "name": "Vahdam Assorted Tea Sampler (10 teas)",
     "price": "24.00", "merchant": "Vahdam Teas",
     "merchant_url": "https://www.vahdamteas.com", "reason": "she loves tea"},
    {"id": "leuchtturm_notebook", "name": "Leuchtturm1917 Hardcover Notebook",
     "price": "22.00", "merchant": "Leuchtturm1917",
     "merchant_url": "https://www.leuchtturm1917.com", "reason": "for her reading notes"},
]


async def fake_suggest(context, budget, n=3):
    calls["suggest"] = (context, budget)
    return PICKS


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


def test_parse_budget():
    assert gifts.parse_budget("$50") == 50.0
    assert gifts.parse_budget("around 40 dollars") == 40.0
    assert gifts.parse_budget("under 1,200") == 1200.0
    assert gifts.parse_budget("no idea") is None


def test_catalog_wellformed():
    """A bad price or missing merchant would only surface as a Prava 400 at runtime."""
    for item in gifts.CATALOG:
        assert float(item["price"]) > 0, item["id"]
        assert item["merchant_url"].startswith("https://"), item["id"]
        assert len(f"gift:{item['id']}".encode()) <= 64, f"{item['id']} exceeds callback_data limit"


def test_price_parsing():
    assert search._parse_price("$24.99") == 24.99
    assert search._parse_price("1,299.00 INR") == 1299.0
    assert search._parse_price("free") is None
    assert search._parse_price(None) is None


def test_live_item_conversion():
    """Malformed shopping results must be dropped, never forwarded to Prava."""
    ok = search._to_item(
        {"title": "Vahdam Tea Sampler", "price": "$24.99",
         "source": "Vahdam", "link": "https://vahdam.com/p/1"}, 0)
    assert ok["price"] == "24.99" and ok["merchant"] == "Vahdam"
    assert len(f"gift:{ok['id']}".encode()) <= 64

    bad = [
        {"title": "x", "price": "$1", "source": "y", "link": "http://insecure.com"},
        {"title": "", "price": "$1", "source": "y", "link": "https://a.com"},
        {"title": "x", "price": None, "source": "y", "link": "https://a.com"},
        {"title": "x", "price": "$1", "source": "", "link": "https://a.com"},
    ]
    assert all(search._to_item(b, 0) is None for b in bad), "bad rows must be dropped"

    long_title = search._to_item(
        {"title": "A" * 300, "price": "$5", "source": "M", "link": "https://a.com"}, 9)
    assert len(f"gift:{long_title['id']}".encode()) <= 64, "id must stay callback-safe"


async def test_search_without_key_is_silent():
    os.environ.pop("SERPER_API_KEY", None)
    assert await search.find_products("tea", 50.0) == [], "no key must fall back, not raise"


def test_affordable_never_empty():
    assert gifts._affordable(5.0), "must fall back rather than show an empty menu"
    assert all(float(i["price"]) <= 30 for i in gifts._affordable(30.0))


def _signed(secret_key: bytes, webhook_id: str, timestamp: str, body: bytes) -> str:
    import base64, hashlib, hmac
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(secret_key, signed, hashlib.sha256).digest()).decode()


def test_webhook_signature_verification():
    """An unsigned or forged webhook must never reach the payment flow."""
    import base64
    key = b"0123456789abcdef0123456789abcdef"
    secret = "whsec_" + base64.b64encode(key).decode()
    body = b'{"event_type":"message.received"}'
    now = str(int(time.time()))

    good = {"webhook-id": "evt_1", "webhook-timestamp": now,
            "webhook-signature": _signed(key, "evt_1", now, body)}
    assert linq_webhook.verify_signature(good, body, secret)

    forged = dict(good, **{"webhook-signature": "v1,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})
    assert not linq_webhook.verify_signature(forged, body, secret), "forged sig accepted"

    tampered = linq_webhook.verify_signature(good, body + b"x", secret)
    assert not tampered, "body tampering not detected"

    stale = dict(good, **{"webhook-timestamp": str(int(time.time()) - 600)})
    stale["webhook-signature"] = _signed(key, "evt_1", stale["webhook-timestamp"], body)
    assert not linq_webhook.verify_signature(stale, body, secret), "replay window not enforced"

    assert not linq_webhook.verify_signature({}, body, secret), "missing headers accepted"


def test_extract_real_linq_payload():
    """Shape captured from a live Linq webhook — the docs implied data.from, which
    does not exist, and guessing it silently dropped every inbound message."""
    live = {
        "api_version": "v3",
        "event_type": "message.received",
        "data": {
            "chat": {"id": "e1d73c85-2191-425a-b1bb-ea3e891316c2", "is_group": False},
            "direction": "inbound",
            "id": "d2d2c5a9-7b1a-4ebf-9c7f-f33ce42770d9",
            "parts": [{"type": "text", "value": "Hey", "text_decorations": None}],
            "sender_handle": {"handle": "+918527809319", "is_me": False},
        },
    }
    assert linq_webhook.extract(live) == ("+918527809319", "Hey")

    # Our own outbound echo must not be treated as user input.
    echo = {"event_type": "message.received",
            "data": dict(live["data"], direction="outbound")}
    assert linq_webhook.extract(echo) is None

    assert linq_webhook.extract({"event_type": "message.delivered"}) is None
    assert linq_webhook.extract({"event_type": "message.received", "data": {}}) is None


def test_keyboard_becomes_numbered_list():
    """iMessage has no buttons, so options must survive as numbered text."""
    phone = "+15551234567"
    response = agent.BotResponse("Pick one:", keyboard=[("Tea — $24", "gift:tea"),
                                                        ("Book — $22", "gift:book")])
    text = linq_webhook.render(response, phone)
    assert "1. Tea — $24" in text and "2. Book — $22" in text
    assert "Reply with a number (1-2)" in text

    assert linq_webhook.resolve(phone, "2") == "gift:book"
    assert linq_webhook.resolve(phone, " 1 ") == "gift:tea"
    assert linq_webhook.resolve(phone, "9") == "9", "out of range must pass through"
    assert linq_webhook.resolve(phone, "hello") == "hello"
    assert linq_webhook.resolve("+15559999999", "1") == "1", "unknown sender has no options"


def test_checkout_url_included_as_link():
    text = linq_webhook.render(
        agent.BotResponse("Pay now", checkout_url="https://sandbox.collect.prava.space?session=x"),
        "+15551112222")
    assert "https://sandbox.collect.prava.space?session=x" in text


def test_next_birthday_rolls_over():
    today = date(2026, 8, 2)
    # Birth year in the past — next occurrence is this year if still ahead.
    assert db.next_birthday("1999-12-25", today) == date(2026, 12, 25)
    # Already passed this year — roll to next.
    assert db.next_birthday("1999-01-05", today) == date(2027, 1, 5)
    # Today counts as upcoming, not missed.
    assert db.next_birthday("1990-08-02", today) == date(2026, 8, 2)
    assert db.next_birthday("garbage", today) is None
    assert db.next_birthday(None, today) is None


async def test_scheduler_sends_once_and_only_to_reachable_users():
    uid = db.upsert_user("+15550001111", "linq")
    birthday = (date.today() + timedelta(days=6)).isoformat()
    with db.get_conn() as conn:
        friend_id = conn.execute(
            "INSERT INTO friends (user_id, name, birthday) VALUES (?, ?, ?)",
            (uid, "Priya", birthday),
        ).lastrowid

    assert db.schedule_reminder(friend_id, birthday) == date.today().isoformat()

    assert any(r["friend_id"] == friend_id for r in db.due_reminders("linq"))
    # A Telegram worker must not claim a reminder it cannot deliver.
    assert not any(r["friend_id"] == friend_id for r in db.due_reminders("telegram"))

    sent, primed = [], []

    async def send(to, text):
        sent.append((to, text))

    await scheduler.dispatch_due("linq", send, lambda uid_, row: primed.append(uid_))
    assert sent and "Priya" in sent[0][1], sent
    assert sent[0][0] == "+15550001111"
    assert primed == ["+15550001111"], "session must be primed or the reply is orphaned"

    # Running again must not resend — claiming is what prevents duplicates.
    sent.clear()
    await scheduler.dispatch_due("linq", send)
    assert not any("Priya" in t for _, t in sent), "reminder was sent twice"

    # Next year's reminder should already be queued.
    with db.get_conn() as conn:
        pending = conn.execute(
            "SELECT fire_date FROM reminders WHERE friend_id = ? AND status = 'pending'",
            (friend_id,),
        ).fetchall()
    assert len(pending) == 1 and pending[0]["fire_date"] > date.today().isoformat()


def test_reminder_wording():
    today = date(2026, 8, 2)
    assert "in 6 days" in scheduler.reminder_text("Ashna", "1999-08-08", today)
    assert "tomorrow" in scheduler.reminder_text("Ashna", "1999-08-03", today)
    assert "today" in scheduler.reminder_text("Ashna", "1999-08-02", today)


def test_clean_friends_rejects_bad_input():
    """This payload comes from the open web — malformed entries must be dropped."""
    good = onboarding._clean_friends([
        {"name": "Ashna", "date": "2026-08-07"},
        {"name": "  Ravi  ", "date": "1999-12-01"},
    ])
    assert [f["name"] for f in good] == ["Ashna", "Ravi"], good

    bad = onboarding._clean_friends([
        {"name": "", "date": "2026-08-07"},
        {"name": "NoDate"},
        {"name": "Bad", "date": "07/08/2026"},
        {"name": "Bad2", "date": "not-a-date"},
        "not a dict",
        {"name": "Bad3", "date": "20260807"},
    ])
    assert bad == [], bad

    assert onboarding._clean_friends("nonsense") == []
    assert onboarding._clean_friends(None) == []

    # Oversized name truncated, not rejected outright.
    long_name = onboarding._clean_friends([{"name": "A" * 500, "date": "2026-01-01"}])
    assert len(long_name[0]["name"]) <= 60

    # Flood protection.
    many = onboarding._clean_friends(
        [{"name": "P%d" % i, "date": "2026-01-01"} for i in range(500)])
    assert len(many) <= 50


def test_setup_api_requires_a_valid_friend():
    status, body = onboarding.create_setup_token(b'{"friends": []}')
    assert status == 400, body
    status, _ = onboarding.create_setup_token(b'not json')
    assert status == 400
    status, _ = onboarding.create_setup_token(b'{"friends":[{"name":"X","date":"2026-01-01"}]}')
    assert status == 200


def test_setup_token_is_single_use():
    token = db.create_setup({"friends": [{"name": "Meera", "date": "2026-09-09"}], "cap": 40})
    uid = db.upsert_user("web_user_1")

    payload = db.claim_setup(token, uid)
    assert payload and payload["cap"] == 40
    assert [f["name"] for f in db.list_friends(uid)] == ["Meera"]

    # A replayed link must not re-apply.
    assert db.claim_setup(token, uid) is None
    assert db.claim_setup("never-issued", uid) is None


async def test_start_with_token_onboards_user():
    token = db.create_setup(
        {"friends": [{"name": "Dev", "date": "2026-10-10"}], "cap": 60})
    reply = await agent.handle(
        agent.NormalizedEvent(user_id="tg_web_1", platform="test", text="/start " + token))
    assert "connected" in reply.text.lower(), reply.text
    assert "Dev" in reply.text
    assert agent._session("tg_web_1")["budget"] == 60.0

    replay = await agent.handle(
        agent.NormalizedEvent(user_id="tg_web_2", platform="test", text="/start " + token))
    assert "already used" in replay.text.lower(), replay.text

    plain = await agent.handle(
        agent.NormalizedEvent(user_id="tg_web_3", platform="test", text="/start"))
    assert "Welcome" in plain.text


async def test_poll_survives_transient_outage():
    """Prava's sandbox 504s intermittently; a blip must not abandon a live payment."""
    attempts = {"n": 0}
    real = prava.get_payment_result

    async def flaky(session_id):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise prava.PravaUnavailable("HTTP 504")
        return ready_result()

    prava.get_payment_result = flaky
    try:
        result = await prava.poll_until_ready("ses_x", interval=0, timeout=5)
        assert result is not None, "must keep polling through a transient outage"
        assert attempts["n"] == 3
    finally:
        prava.get_payment_result = real


async def drive_to_gift_choice(user):
    """/start -> /test -> interests -> budget -> picks shown."""
    ev = lambda t: agent.NormalizedEvent(user_id=user, platform="test", text=t)
    assert "Welcome" in (await agent.handle(ev("/start"))).text
    assert "birthday" in (await agent.handle(ev("/test"))).text
    assert "budget" in (await agent.handle(ev("she loves books and tea"))).text.lower()

    reply = await agent.handle(ev("$50"))
    assert calls["suggest"] == ("she loves books and tea", 50.0), calls["suggest"]
    assert reply.keyboard == [
        ("Vahdam Assorted Tea Sampler (10 teas) — $24.00", "gift:vahdam_tea_sampler"),
        ("Leuchtturm1917 Hardcover Notebook — $22.00", "gift:leuchtturm_notebook"),
    ], reply.keyboard
    assert "she loves tea" in reply.text
    return ev


async def test_happy_path():
    ev = await drive_to_gift_choice("u1")

    reply = await agent.handle(ev("gift:vahdam_tea_sampler"))
    assert reply.poll_session_id == "ses_test123"
    assert reply.checkout_url.startswith("https://")
    assert calls["create"]["amount"] == "24.00", calls["create"]
    # Real merchant now reaches Prava, not a placeholder.
    assert calls["create"]["merchant"]["name"] == "Vahdam Teas"
    assert calls["create"]["merchant"]["url"] == "https://www.vahdamteas.com"
    assert agent._session("u1")["state"] == "AWAITING_PAYMENT"

    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, ready_result())
    reply = await agent.await_payment("u1")

    assert "sandbox payment completed" in reply.text, reply.text
    assert "Vahdam Teas" in reply.text
    # Without report-status the Prava session never reaches "completed".
    assert calls["report"] == ("ses_test123", "tli_001", "APPROVED"), calls.get("report")
    assert agent._session("u1")["state"] == "CONFIRMED"


async def test_timeout_returns_to_gift_picker():
    ev = await drive_to_gift_choice("u2")
    await agent.handle(ev("gift:vahdam_tea_sampler"))

    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, None)
    reply = await agent.await_payment("u2")

    assert "timed out" in reply.text, reply.text
    assert reply.keyboard, "must re-offer gifts"
    assert agent._session("u2")["state"] == "SHOWING_GIFTS"


async def test_failed_payment_does_not_record_order():
    ev = await drive_to_gift_choice("u3")
    await agent.handle(ev("gift:leuchtturm_notebook"))
    calls.pop("report", None)

    failed = {"status": "failed", "transactions": []}
    agent.prava.poll_until_ready = lambda sid, **kw: asyncio.sleep(0, failed)
    reply = await agent.await_payment("u3")

    assert "did not complete" in reply.text, reply.text
    assert "report" not in calls, "must not settle a failed payment"


async def test_stale_button_reshows_gifts():
    """Tapping a button from an old session must not crash."""
    ev = await drive_to_gift_choice("u4")
    reply = await agent.handle(ev("gift:no_such_id"))
    assert reply.keyboard, "unknown id should re-show the picks"


async def main():
    db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
    db.init_db()
    agent.gifts.suggest = fake_suggest
    agent.prava.create_session = fake_create_session
    agent.prava.report_status = fake_report_status

    test_parse_budget()
    test_catalog_wellformed()
    test_price_parsing()
    test_live_item_conversion()
    await test_search_without_key_is_silent()
    test_affordable_never_empty()
    test_next_birthday_rolls_over()
    await test_scheduler_sends_once_and_only_to_reachable_users()
    test_reminder_wording()
    test_clean_friends_rejects_bad_input()
    test_setup_api_requires_a_valid_friend()
    test_setup_token_is_single_use()
    await test_start_with_token_onboards_user()
    test_webhook_signature_verification()
    test_extract_real_linq_payload()
    test_keyboard_becomes_numbered_list()
    test_checkout_url_included_as_link()
    await test_poll_survives_transient_outage()
    await test_happy_path()
    await test_timeout_returns_to_gift_picker()
    await test_failed_payment_does_not_record_order()
    await test_stale_button_reshows_gifts()
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
