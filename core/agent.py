import logging
import os
import re
from dataclasses import dataclass, field

from core import gifts, prava
from core.db import (
    claim_setup,
    create_order,
    get_spend_cap,
    resume_reminder,
    save_context,
    seed_ashna,
    upsert_user,
)

logger = logging.getLogger(__name__)

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


def _user_email(user_id: str) -> str:
    """Email to attach to a Prava session.

    Neither Telegram nor Linq exposes an email address. example.com is
    RFC-reserved and undeliverable, which card networks can reject when issuing
    credentials, so GIFTBOT_USER_EMAIL should hold a real address.
    """
    configured = os.getenv("GIFTBOT_USER_EMAIL")
    if configured:
        return configured
    handle = re.sub(r"[^a-z0-9]", "", user_id.lower()) or "user"
    return f"giftbot.{handle}@gmail.com"


def begin_reminder(user_id: str, reminder: dict) -> None:
    """Prime a session so the reply to a proactive reminder lands in the flow.

    Without this the user answers "she likes tea" and the bot, having no session,
    responds "Type /start or /test to begin."
    """
    sess = _session(user_id)
    sess.update({"state": "REMINDED", "friend_id": reminder["friend_id"]})


async def handle(event: NormalizedEvent) -> BotResponse:
    db_user_id = upsert_user(event.user_id, event.platform)
    sess = _session(event.user_id)
    state = sess["state"]
    text = (event.text or "").strip()

    if text.startswith("/start"):
        sess["state"] = "IDLE"
        token = text[len("/start"):].strip()
        if not token:
            return BotResponse("Welcome to GiftBot! Use /test to try a gift reminder.")

        payload = claim_setup(token, db_user_id)
        if payload is None:
            # Tokens are single-use, so a second tap of the same link lands here.
            logger.info("unclaimable setup token from %s", event.user_id)
            return BotResponse(
                "That setup link was already used or has expired. "
                "Run through the setup page again for a fresh one."
            )

        friends = payload.get("friends", [])
        if payload.get("cap"):
            sess["budget"] = float(payload["cap"])
        logger.info("claimed setup for %s with %d friend(s)", event.user_id, len(friends))

        listed = "\n".join(f"• {f['name']} — {f['date']}" for f in friends[:10])
        extra = f"\n…and {len(friends) - 10} more" if len(friends) > 10 else ""
        budget_line = (
            f"\n\nSpending limit: ${float(payload['cap']):.0f} per gift."
            if payload.get("cap") else ""
        )
        return BotResponse(
            f"✅ You're connected! I've saved {len(friends)} birthday"
            f"{'s' if len(friends) != 1 else ''}:\n\n{listed}{extra}{budget_line}\n\n"
            f"I'll message you when one is coming up. Try /test to see how it works."
        )

    if text == "/test":
        friend_id = seed_ashna(db_user_id)
        sess.update({"state": "REMINDED", "friend_id": friend_id})
        return BotResponse("🎂 Ashna's birthday is in 6 days! What are her interests or hobbies?")

    if state == "REMINDED":
        return await _accept_context(sess, db_user_id, sess["friend_id"], text)

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
            user_email=_user_email(event.user_id),
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
        logger.info(
            "created prava session %s for %s (%s, $%s)",
            session["session_id"], event.user_id, item["merchant"], amount,
        )
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

    # Nothing in memory — but a reminder may have gone out from a process that
    # has since restarted. Recover from the database instead of stranding them.
    if text and not text.startswith("/"):
        recent = resume_reminder(db_user_id)
        if recent:
            logger.info("resuming reminder %s for %s after lost session",
                        recent["id"], event.user_id)
            sess["friend_id"] = recent["friend_id"]
            return await _accept_context(sess, db_user_id, recent["friend_id"], text)

    return BotResponse("Type /start or /test to begin.")


async def _accept_context(sess: dict, db_user_id: int, friend_id: int, text: str) -> BotResponse:
    """Store what we learned about the friend, then ask budget or go straight to gifts."""
    save_context(friend_id, text)
    sess["context"] = text
    # Someone who set a cap during onboarding has already answered this.
    cap = get_spend_cap(db_user_id)
    if cap:
        sess["budget"] = cap
        reply = await _show_gifts(sess)
        return BotResponse(
            f"Got it — working to your ${cap:.0f} limit.\n\n{reply.text}",
            keyboard=reply.keyboard,
        )
    sess["state"] = "ASKING_BUDGET"
    return BotResponse("Got it. What's your budget? (e.g. $50)")


async def await_payment(user_id: str) -> BotResponse:
    """Poll Prava until the cardholder approves, then settle and record the order."""
    sess = _session(user_id)
    session_id = sess["session_id"]

    logger.info("polling prava session %s for %s", session_id, user_id)
    result = await prava.poll_until_ready(session_id)
    if result is None:
        logger.warning("session %s timed out with no terminal status", session_id)
        return await _retry(sess, "⏳ Checkout timed out.")

    logger.info("session %s reached status=%s", session_id, result.get("status"))
    line_item = prava.first_credentialed_line_item(result)
    if result.get("status") == "failed" or line_item is None:
        logger.warning("session %s failed: %s", session_id, result)
        return await _retry(sess, "❌ Prava checkout did not complete.")

    # Prava issues a one-time card scoped to this merchant; actually charging the
    # merchant is the caller's job and needs UCP/production, so this reports the
    # sandbox outcome only. See gifts.json for the UCP upgrade path.
    await prava.report_status(session_id, line_item["txn_ref_id"], "APPROVED")
    logger.info("settled session %s txn %s", session_id, line_item["txn_ref_id"])

    create_order(
        sess["friend_id"], sess["pending_gift"], sess["pending_amount"], line_item["txn_ref_id"]
    )
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
