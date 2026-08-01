# GiftBot Phase 1 Design

**Date:** 2026-08-01  
**Scope:** Conversation state machine, /test command, context collection, gift selection, mock payment

## State Machine

Stored in `_sessions: dict[str, dict]` in `core/agent.py` — in-memory, keyed by `user_id`.

```
IDLE → /test → REMINDED → (user types context) → SHOWING_GIFTS → (taps gift) → PAYING → CONFIRMED
```

Each session entry: `{"state": "COLLECTING", "friend_id": 3, "pending_gift": ..., "pending_amount": ...}`

## /test Flow

1. Seed Ashna (INSERT OR IGNORE, birthday = today+6) → state=REMINDED
2. Bot asks for her interests
3. User replies → save to `friends.context` → state=SHOWING_GIFTS, send 3 inline buttons
4. User taps button (callback_query) → state=PAYING, bot says "Processing..."
5. Adapter sleeps 2s, calls `complete_payment(user_id)` → INSERT orders, UPDATE friends.last_gift → state=CONFIRMED
6. Bot sends confirmation

## BotResponse Extension

```python
@dataclass
class BotResponse:
    text: str
    keyboard: list[tuple[str, str]] | None = None  # (label, callback_data) — adapter builds InlineKeyboardMarkup
    payment_pending: bool = False  # adapter handles 2s delay + complete_payment()
```

Core remains Telegram-free. Async delay lives in the adapter.

## Gift Options

| Label | callback_data |
|---|---|
| Cozy Reading Kit — $25 | gift:reading:25 |
| Gourmet Basket — $45 | gift:gourmet:45 |
| Spa Experience — $70 | gift:spa:70 |

## New DB Helpers

- `seed_ashna(user_id)` → INSERT OR IGNORE, return friend_id
- `save_context(friend_id, context)` → UPDATE friends.context
- `create_order(friend_id, gift, amount)` → INSERT orders + UPDATE friends.last_gift

## Files Changed

- `core/db.py` — add 3 helpers
- `core/agent.py` — state machine, GIFTS list, `complete_payment()`
- `adapters/telegram/handlers.py` — add `on_callback`, `_send` helper, payment flow
- `main.py` — register `CallbackQueryHandler`
