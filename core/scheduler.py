"""Birthday reminder scheduler.

This is what makes GiftBot an agent rather than a chat command: without it the
bot only ever replies, and the "we'll message you when a birthday is coming up"
promise is untrue.

Each adapter process runs its own loop for its own platform, so a Telegram user
is never claimed by the Linq process, which could not reach them. Claiming is
atomic on the reminders row, so overlapping runs cannot double-send.
"""

import asyncio
import logging
from datetime import date, timedelta

from core.db import claim_reminder, due_reminders, next_birthday, schedule_reminder

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 300.0  # 5 minutes; birthdays are not time-critical to the second


def reminder_text(name: str, birthday: str, today: date | None = None) -> str:
    upcoming = next_birthday(birthday, today)
    days = (upcoming - (today or date.today())).days if upcoming else None
    if days is None:
        when = "coming up"
    elif days <= 0:
        when = "today"
    elif days == 1:
        when = "tomorrow"
    else:
        when = f"in {days} days"
    return (
        f"🎂 {name}'s birthday is {when}!\n\n"
        f"Want me to find a gift? Tell me what they're into."
    )


async def dispatch_due(platform: str, send, on_reminder=None) -> int:
    """Send every due reminder for this platform. Returns how many went out.

    send(external_id, text) delivers the message; on_reminder(external_id, friend)
    lets the adapter prime its conversation state so the user's reply lands in
    the right place.
    """
    sent = 0
    for row in due_reminders(platform):
        # Claim before sending: if this fails another worker already has it.
        if not claim_reminder(row["id"]):
            continue
        try:
            if on_reminder:
                on_reminder(row["external_id"], row)
            await send(row["external_id"], reminder_text(row["name"], row["birthday"]))
            sent += 1
            logger.info(
                "reminder sent: %s -> %s (%s)", platform, row["external_id"], row["name"]
            )
            # Queue the following year from just past this birthday. Using today
            # would re-select the same occurrence and resend on every tick.
            handled = next_birthday(row["birthday"])
            if handled:
                schedule_reminder(
                    row["friend_id"], row["birthday"], from_date=handled + timedelta(days=1)
                )
        except Exception:
            logger.exception("failed sending reminder %s", row["id"])
    return sent


async def run(platform: str, send, on_reminder=None, interval: float = CHECK_INTERVAL) -> None:
    """Poll for due reminders forever. Never let one bad tick kill the loop."""
    logger.info("scheduler running for %s (every %.0fs)", platform, interval)
    while True:
        try:
            await dispatch_due(platform, send, on_reminder)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(interval)
