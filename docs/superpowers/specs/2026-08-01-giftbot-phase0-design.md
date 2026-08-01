# GiftBot Phase 0 Design

**Date:** 2026-08-01  
**Stack:** Python, FastAPI, SQLite, python-telegram-bot  
**Scope:** Project scaffold, DB schema, webhook router, /start response

## Architecture

```
GiftBot/
├── core/               # pure domain logic — never imports from adapters
│   ├── agent.py        # handle(NormalizedEvent) → BotResponse
│   └── db.py           # schema init + CRUD helpers
├── adapters/
│   └── telegram/
│       ├── bot.py      # Bot instance, register_webhook()
│       └── webhook.py  # POST /webhook → NormalizedEvent → core
├── main.py             # FastAPI app, lifespan: init_db + register_webhook
├── .env.example
└── requirements.txt
```

Core boundary: `core/` defines `NormalizedEvent` and `BotResponse`. Adapters translate to/from these types. Core never calls Telegram APIs.

## Data Flow

```
Telegram → POST /webhook
  → adapters/telegram/webhook.py parses raw dict
  → NormalizedEvent(user_id, platform, text, payload)
  → core/agent.handle()
  → BotResponse(text)
  → adapters/telegram/webhook.py calls bot.send_message()
```

## DB Schema (SQLite, 4 tables)

```sql
users     (id PK, telegram_id UNIQUE, platform, prava_token)
friends   (id PK, user_id FK→users, name, birthday, context, last_gift)
reminders (id PK, friend_id FK→friends, fire_date, status)
orders    (id PK, friend_id FK→friends, gift, amount, prava_txn, status)
```

Created on startup via `core/db.init_db()`.

## Webhook Registration

`WEBHOOK_URL` from `.env`. `main.py` lifespan calls `register_webhook(WEBHOOK_URL)` on startup.  
Phase 0–1: ngrok URL. Phase 3: Railway URL. Only the env var changes.

## Phase 0 Success Criteria

- `/start` → bot replies with greeting, user upserted in `users` table
- DB file created on first run
- Webhook auto-registered on startup
