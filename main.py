import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from core import scheduler
from core.agent import begin_reminder
from core.db import init_db
from adapters.telegram.handlers import on_message, on_callback

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_error(update, context) -> None:
    logger.error("unhandled error: %s", context.error, exc_info=context.error)


async def _start_scheduler(application) -> None:
    """Run the reminder loop alongside polling.

    It lives in this process so it shares the in-memory conversation state — a
    reminder sent from elsewhere would leave the bot unable to interpret the
    reply. Only 'telegram' users are claimed here; Linq's process takes its own.
    """
    async def send(chat_id: str, text: str) -> None:
        await application.bot.send_message(chat_id=chat_id, text=text)

    # asyncio rather than application.create_task: this loop runs for the life of
    # the process and is never awaited, which PTB's task tracking warns about.
    asyncio.create_task(scheduler.run("telegram", send, begin_reminder))


def main():
    init_db()
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .post_init(_start_scheduler)
        .build()
    )
    app.add_handler(CommandHandler(["start", "test"], on_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    logger.info("GiftBot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
