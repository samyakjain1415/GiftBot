import os
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from core.db import init_db
from adapters.telegram.handlers import on_message, on_callback

load_dotenv()


def main():
    init_db()
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "test"], on_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
