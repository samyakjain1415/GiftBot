import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from core.agent import NormalizedEvent, BotResponse, handle, complete_payment


def _build_keyboard(keyboard: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data)]
        for label, data in keyboard
    ])


async def _send(update: Update, response: BotResponse) -> None:
    markup = _build_keyboard(response.keyboard) if response.keyboard else None
    target = update.message or (update.callback_query and update.callback_query.message)
    if target:
        await target.reply_text(response.text, reply_markup=markup)


async def _handle_and_respond(update: Update, user_id: str, text: str) -> None:
    event = NormalizedEvent(user_id=user_id, platform="telegram", text=text)
    response = handle(event)
    await _send(update, response)
    if response.payment_pending:
        await asyncio.sleep(2)
        await _send(update, complete_payment(user_id))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    await _handle_and_respond(update, str(msg.from_user.id), msg.text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _handle_and_respond(update, str(query.from_user.id), query.data)
