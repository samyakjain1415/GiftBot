from dataclasses import dataclass, field

from core import gifts, prava
from core.db import upsert_user, seed_ashna, save_context, create_order

_sessions: dict[str, dict] = {}


@dataclass
class NormalizedEvent:
    user_id: str
    platform: str
    text: str | None
    payload: dict = field(default_factory=dict)


@dataclass
class BotResponse:
    text: str
    keyboard: list[tuple[str, str]] | None = None
    checkout_url: str | None = None
    poll_session_id: str | None = None


def _session(user_id: str) -> dict:
    if user_id not in _sessions:
        _sessions[user_id] = {"state": "IDLE"}
    return _sessions[user_id]


def _keyboard(picks: list[dict]) -> list[tuple[str, str]]:
    return [(f"{p['name']} — ${p['price']}", f"gift:{p['id']}") for p in picks]


def _picks_text(picks: list[dict]) -> str:
    lines = [f"• *{p['name']}* (${p['price']}, {p['merchant']}) — {p['reason']}" for p in picks]
    return "Here's what I'd pick:\n\n" + "\n".join(lines)


async def _show_gifts(sess: dict) -> BotResponse:
    picks = await gifts.suggest(sess.get("context", ""), sess.get("budget"))
    sess["picks"] = {p["id"]: p for p in picks}
    sess["state"] = "SHOWING_GIFTS"
    return BotResponse(_picks_text(picks), keyboard=_keyboard(picks))


async def handle(event: NormalizedEvent) -> BotResponse:
    db_user_id = upsert_user(event.user_id)
    sess = _session(event.user_id)
    state = sess["state"]
    text = (event.text or "").strip()

    if text == "/start":
        sess["state"] = "IDLE"
        return BotResponse("Welcome to GiftBot! Use /test to try a gift reminder.")

    if text == "/test":
        friend_id = seed_ashna(db_user_id)
        sess.update({"state": "REMINDED", "friend_id": friend_id})
        return BotResponse("🎂 Ashna's birthday is in 6 days! What are her interests or hobbies?")

    if state == "REMINDED":
        save_context(sess["friend_id"], text)
        sess.update({"state": "ASKING_BUDGET", "context": text})
        return BotResponse("Got it. What's your budget? (e.g. $50)")

    if state == "ASKING_BUDGET":
        sess["budget"] = gifts.parse_budget(text)
        return await _show_gifts(sess)

    if state == "SHOWING_GIFTS" and text.startswith("gift:"):
        item = sess.get("picks", {}).get(text.split(":", 1)[1])
        if item is None:
            return await _show_gifts(sess)

        amount = f"{float(item['price']):.2f}"
        session = await prava.create_session(
            user_id=event.user_id,
            # Telegram exposes no email; sandbox only needs a well-formed one.
            user_email=f"tg{event.user_id}@example.com",
            amount=amount,
            description=item["name"],
            merchant={
                "name": item["merchant"],
                "url": item["merchant_url"],
                "country_code_iso2": "US",
            },
        )
        sess.update({
            "state": "AWAITING_PAYMENT",
            "pending_gift": item["name"],
            "pending_merchant": item["merchant"],
            "pending_amount": float(item["price"]),
            "session_id": session["session_id"],
        })
        return BotResponse(
            f"🔐 Prava sandbox checkout\n\n"
            f"{item['name']} — ${amount} from {item['merchant']}\n\n"
            f"Tap below to pay. Use your sandbox test card, OTP 456789, "
            f"then approve with Face ID / Touch ID.\n"
            f"Link expires in 15 minutes.",
            checkout_url=session["iframe_url"],
            poll_session_id=session["session_id"],
        )

    if state == "AWAITING_PAYMENT":
        return BotResponse("Still waiting on the Prava checkout — finish it in the browser tab.")

    if state == "CONFIRMED":
        return BotResponse("Gift already sent! Type /test to try again.")

    return BotResponse("Type /start or /test to begin.")


async def await_payment(user_id: str) -> BotResponse:
    """Poll Prava until the cardholder approves, then settle and record the order."""
    sess = _session(user_id)
    session_id = sess["session_id"]

    result = await prava.poll_until_ready(session_id)
    if result is None:
        return await _retry(sess, "⏳ Checkout timed out.")

    line_item = prava.first_credentialed_line_item(result)
    if result.get("status") == "failed" or line_item is None:
        return await _retry(sess, "❌ Prava checkout did not complete.")

    # Prava issues a one-time card scoped to this merchant; actually charging the
    # merchant is the caller's job and needs UCP/production, so this reports the
    # sandbox outcome only. See gifts.json for the UCP upgrade path.
    await prava.report_status(session_id, line_item["txn_ref_id"], "APPROVED")

    create_order(sess["friend_id"], sess["pending_gift"], sess["pending_amount"])
    sess["state"] = "CONFIRMED"
    return BotResponse(
        f"✅ Prava sandbox payment completed — test mode\n\n"
        f"🎁 {sess['pending_gift']} for Ashna\n"
        f"🏪 {sess['pending_merchant']}\n"
        f"Session: {session_id}\n"
        f"Txn ref: {line_item['txn_ref_id']}\n\n"
        f"_Sandbox transaction — no real funds moved and no merchant order was placed._"
    )


async def _retry(sess: dict, reason: str) -> BotResponse:
    response = await _show_gifts(sess)
    return BotResponse(f"{reason} Pick again:", keyboard=response.keyboard)
