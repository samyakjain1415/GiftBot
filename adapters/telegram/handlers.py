import logging
from html import escape

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from core.agent import NormalizedEvent, BotResponse, handle, await_payment
from core.prava import PravaUnavailable

logger = logging.getLogger(__name__)

OUTAGE = (
    "⚠️ Prava's sandbox isn't responding right now (their gateway is timing out). "
    "This is on their side, not your payment — nothing was charged. Try again shortly."
)


def _markup(response: BotResponse) -> InlineKeyboardMarkup | None:
    if response.checkout_url:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Pay with Prava", url=response.checkout_url)]]
        )
    if response.keyboard:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data)] for label, data in response.keyboard]
        )
    return None


CAPTION_LIMIT = 1024   # Telegram's cap on photo captions


def _caption(card) -> str:
    """Card as HTML. Every value is escaped — product titles come from the open
    web and a stray & or < would otherwise reject the whole message."""
    lines = [f"<b>{escape(card.title)}</b>", escape(card.subtitle)]
    if card.blurb:
        lines += ["", escape(card.blurb)]
    if card.reason:
        lines += ["", f"✨ {escape(card.reason)}"]
    if card.link_url:
        lines += ["", f'<a href="{escape(card.link_url, quote=True)}">🔗 View product</a>']
    text = "\n".join(lines)
    return text if len(text) <= CAPTION_LIMIT else text[:CAPTION_LIMIT - 1] + "…"


async def _send_card(target, card) -> None:
    """One product: photo with a caption and its own button.

    Falls back to text if there is no image or the photo cannot be sent —
    gstatic thumbnail URLs expire, and losing the product would be worse than
    losing the picture.
    """
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(card.action_label, callback_data=card.action_data)]]
    )
    caption = _caption(card)

    if card.image_url:
        try:
            await target.reply_photo(
                photo=card.image_url, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            return
        except TelegramError as exc:
            logger.info("photo failed for %s (%s); sending as text", card.title[:40], exc)

    await target.reply_text(
        caption, parse_mode=ParseMode.HTML, reply_markup=markup,
        disable_web_page_preview=False,
    )


async def _send(update: Update, response: BotResponse) -> None:
    target = update.message or (update.callback_query and update.callback_query.message)
    if not target:
        return
    if response.text:
        await target.reply_text(response.text, reply_markup=_markup(response))
    for card in response.cards or []:
        await _send_card(target, card)


async def _handle_and_respond(update: Update, user_id: str, text: str) -> None:
    event = NormalizedEvent(user_id=user_id, platform="telegram", text=text)
    try:
        response = await handle(event)
    except PravaUnavailable as exc:
        logger.warning("prava unavailable for user %s: %s", user_id, exc)
        await _send(update, BotResponse(OUTAGE))
        return
    except Exception:
        logger.exception("handle failed for user %s", user_id)
        await _send(update, BotResponse("⚠️ Something went wrong. Type /test to start over."))
        return

    await _send(update, response)

    if response.poll_session_id:
        try:
            await _send(update, await await_payment(user_id))
        except PravaUnavailable as exc:
            logger.warning("prava unavailable settling for user %s: %s", user_id, exc)
            await _send(update, BotResponse(OUTAGE))
        except Exception:
            logger.exception("payment polling failed for user %s", user_id)
            await _send(update, BotResponse("⚠️ Lost track of that payment. Pick a gift to retry."))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    await _handle_and_respond(update, str(msg.from_user.id), msg.text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        # Telegram redelivers pending callbacks after a restart; those queries are
        # already expired. Acknowledging fails, but the tap is still worth handling.
        logger.info("stale callback query from %s", query.from_user.id)
    await _handle_and_respond(update, str(query.from_user.id), query.data)
