from dataclasses import dataclass, field

from core import prava
from core.db import upsert_user, seed_ashna, save_context, create_order

GIFTS = [
    ("Cozy Reading Kit — $25", "gift:reading:25"),
    ("Gourmet Basket — $45", "gift:gourmet:45"),
    ("Spa Experience — $70", "gift:spa:70"),
]

# ponytail: placeholder merchant — swap #3 (real vendor catalog) replaces this
# with the actual merchant + buy link the token gets charged against.
MERCHANT = {
    "name": "GiftBot Demo Store",
    "url": "https://example.com",
    "country_code_iso2": "US",
}

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
        sess["state"] = "SHOWING_GIFTS"
        return BotResponse("Great! Here are some gift ideas:", keyboard=GIFTS)

    if state == "SHOWING_GIFTS" and text.startswith("gift:"):
        gift_labels = {data: label for label, data in GIFTS}
        _, _, amount_str = text.split(":")
        label = gift_labels.get(text, text)
        amount = f"{float(amount_str):.2f}"
        session = await prava.create_session(
            user_id=event.user_id,
            # Telegram exposes no email; sandbox only needs a well-formed one.
            user_email=f"tg{event.user_id}@example.com",
            amount=amount,
            description=label,
            merchant=MERCHANT,
        )
        sess.update({
            "state": "AWAITING_PAYMENT",
            "pending_gift": label,
            "pending_amount": float(amount_str),
            "session_id": session["session_id"],
        })
        return BotResponse(
            f"🔐 Prava sandbox checkout for {label}\n\n"
            f"Tap below to pay ${amount}. Use a sandbox test card, OTP 456789, "
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
        sess["state"] = "SHOWING_GIFTS"
        return BotResponse("⏳ Checkout timed out. Pick a gift again to retry.", keyboard=GIFTS)

    line_item = prava.first_credentialed_line_item(result)
    if result.get("status") == "failed" or line_item is None:
        sess["state"] = "SHOWING_GIFTS"
        return BotResponse("❌ Prava checkout did not complete. Pick a gift to retry.", keyboard=GIFTS)

    # Prava issues a one-time card; the merchant charge is the caller's job. With a
    # placeholder merchant there is no real charge, so this reports the sandbox
    # outcome only — see MERCHANT above and swap #3.
    await prava.report_status(session_id, line_item["txn_ref_id"], "APPROVED")

    create_order(sess["friend_id"], sess["pending_gift"], sess["pending_amount"])
    sess["state"] = "CONFIRMED"
    return BotResponse(
        f"✅ Prava sandbox payment completed — test mode\n\n"
        f"🎁 {sess['pending_gift']} for Ashna\n"
        f"Session: {session_id}\n"
        f"Txn ref: {line_item['txn_ref_id']}\n\n"
        f"_Sandbox transaction — no real funds moved and no merchant order was placed._"
    )
