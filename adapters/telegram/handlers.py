import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from core.agent import NormalizedEvent, BotResponse, handle, await_payment

logger = logging.getLogger(__name__)


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


async def _send(update: Update, response: BotResponse) -> None:
    target = update.message or (update.callback_query and update.callback_query.message)
    if target:
        await target.reply_text(response.text, reply_markup=_markup(response))


async def _handle_and_respond(update: Update, user_id: str, text: str) -> None:
    event = NormalizedEvent(user_id=user_id, platform="telegram", text=text)
    try:
        response = await handle(event)
    except Exception:
        logger.exception("handle failed for user %s", user_id)
        await _send(update, BotResponse("⚠️ Couldn't reach Prava. Try again in a moment."))
        return

    await _send(update, response)

    if response.poll_session_id:
        try:
            await _send(update, await await_payment(user_id))
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
    await query.answer()
    await _handle_and_respond(update, str(query.from_user.id), query.data)
