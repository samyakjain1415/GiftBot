import logging
import os

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

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


def main():
    init_db()
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "test"], on_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    logger.info("GiftBot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
