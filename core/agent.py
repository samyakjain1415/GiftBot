from dataclasses import dataclass, field
from core.db import upsert_user, seed_ashna, save_context, create_order

GIFTS = [
    ("Cozy Reading Kit — $25", "gift:reading:25"),
    ("Gourmet Basket — $45", "gift:gourmet:45"),
    ("Spa Experience — $70", "gift:spa:70"),
]

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
    payment_pending: bool = False


def _session(user_id: str) -> dict:
    if user_id not in _sessions:
        _sessions[user_id] = {"state": "IDLE"}
    return _sessions[user_id]


def handle(event: NormalizedEvent) -> BotResponse:
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
        sess.update({
            "state": "PAYING",
            "pending_gift": gift_labels.get(text, text),
            "pending_amount": float(amount_str),
        })
        return BotResponse("Processing payment... please wait.", payment_pending=True)

    if state == "CONFIRMED":
        return BotResponse("Gift already sent! Type /test to try again.")

    return BotResponse("Type /start or /test to begin.")


def complete_payment(user_id: str) -> BotResponse:
    sess = _session(user_id)
    create_order(sess["friend_id"], sess["pending_gift"], sess["pending_amount"])
    sess["state"] = "CONFIRMED"
    return BotResponse(f"Done! 🎁 {sess['pending_gift']} ordered for Ashna.")
