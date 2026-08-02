# Conversational Flow Design

**Date:** 2026-08-02
**Scope:** How the bot holds a conversation — off-script input, several
birthdays in flight at once, and a reminder arriving mid-checkout

## Problem

The bot runs a single state machine over a single in-memory session per user:

```python
_sessions: dict[str, dict] = {}   # core/agent.py:22
```

That session holds one `friend_id` and one `state`. Three things break.

**Off-script input falls through.** In `REMINDED`, any text at all is stored as
the friend's context — "what can you do?" becomes an interest. Outside a known
state the reply is `"Type /start or /test to begin."`, which is what a judge
sees the first time they type "hi".

**A second birthday overwrites the first.** Reminders lead the birthday by six
days, so a friend on the 7th is announced on the 1st and a friend on the 10th on
the 4th. If the first gift is not finished by the 4th, `begin_reminder` runs
`sess.update({"state": "REMINDED", "friend_id": ...})` and the session now points
at the second friend. The user's next reply is filed against the wrong person.

**A reminder during checkout corrupts the order.** The same overwrite lands while
`await_payment` is still polling in the background. When the payment settles,

```python
create_order(sess["friend_id"], sess["pending_gift"], ...)   # core/agent.py:256
```

books the gift against whoever the session now names. This is data corruption,
not a display bug, and it is reachable in a normal demo.

Nothing survives a restart either, which matters when the two reminders in the
scenario above are three days apart.

## Constraints

Target is a judged demo a few days out. The bar is that it never looks broken
and never looks robotic; long-horizon architecture is not the goal. Correctness
is in scope only where being wrong is visible — and booking a gift to the wrong
friend is visible.

## Approach

Persisted gift threads, with a thin intent router in front of the existing state
machine. The states themselves are fine; what is wrong is that they live in one
per-user dict instead of one row per gift attempt.

Two alternatives were rejected. Keeping sessions in memory and persisting only
enough to render the collision message is half the work, but leaves both the
restart gap and the wrong-friend order bug. Having Gemini compose every outbound
message reads warmer, but costs a model call per turn and gives up knowing what
will appear on screen during a live demo.

## Data model

New table `threads`, one row per (user, friend) gift attempt:

| Column | Purpose |
|---|---|
| `id`, `user_id`, `friend_id` | identity |
| `state` | `REMINDED`, `ASKING_BUDGET`, `SHOWING_GIFTS`, `AWAITING_PAYMENT`, `CONFIRMED`, `ABANDONED` |
| `budget`, `context` | what the user told us |
| `picks` | JSON of the products offered, so a bare `"2"` still resolves after a restart |
| `session_id`, `pending_gift`, `pending_merchant`, `pending_amount` | the Prava attempt |
| `is_active` | exactly one per user |
| `updated_at` | ordering and staleness |

A partial unique index on `(user_id, friend_id)` where `state` is non-terminal
prevents a re-fired reminder creating a second thread for the same friend.

`_sessions` is deleted. There is no second source of truth.

## Modules

`core/db.py` is already ~400 lines, so threads get their own file.

- **`core/threads.py`** — CRUD plus `active_thread(user_id)`,
  `open_threads(user_id)`, `activate(thread_id)` which clears `is_active` on
  siblings in one transaction. Depends only on `db.get_conn`.
- **`core/intent.py`** — `classify(text, state) -> Intent`. A pure function of
  text and state; no database access, so it tests without fixtures.
- **`core/agent.py`** — loads the active thread, calls `classify`, dispatches.
  The existing transitions are preserved; they read and write a row instead of a
  dict.
- **`core/scheduler.py`** — on a due reminder, finds or creates that friend's
  thread, then sends either a plain reminder or a choice message.

### Interface change

`BotResponse.poll_session_id` becomes `poll_thread_id`, and
`await_payment(user_id)` becomes `await_payment(thread_id)`. Both adapters
(`adapters/telegram/handlers.py`, `adapters/linq/webhook.py`) pass the value
back unchanged.

This is what kills the wrong-friend bug structurally: settlement writes
`create_order` against the friend on *that thread*, so anything arriving in the
meantime cannot retarget it.

## Intent layer

Rules first; Gemini only when they miss.

| Rule match | Intent |
|---|---|
| `gift:<id>` / `thread:<id>` callback data | `PICK` |
| bare digit against the options last offered | `PICK` |
| `parse_budget()` succeeds | `BUDGET` |
| `/start`, `/test`, `/help` | `COMMAND` |
| text matches an open thread's friend name | `PICK_FRIEND` |

The fallback reuses the REST + `responseSchema` call already built for
`gifts.suggest` — no new dependency — constrained to a fixed enum so the model
cannot invent an intent: `REFINE_CHEAPER`, `REFINE_OTHER`, `CONTEXT`,
`SMALL_TALK`, `HELP`, `CANCEL`, `UNKNOWN`.

Three-second timeout. On any failure it degrades to `CONTEXT` when the thread is
`REMINDED` and `UNKNOWN` otherwise, so a Gemini outage costs warmth, not
function.

Actions:

- `REFINE_CHEAPER` — re-run `gifts.suggest` with the budget set to the cheapest
  currently-shown price, minus one cent, so the next set is genuinely cheaper
  rather than merely different. If nothing in the pool is cheaper, say so and
  keep the current picks.
- `REFINE_OTHER` — re-run `gifts.suggest` at the same budget, excluding ids
  already shown in this thread. If the pool is exhausted, say so.
- `PICK_FRIEND` — `threads.activate` that friend's thread and resume it at its
  own state: re-show picks for `SHOWING_GIFTS`, re-ask interests for `REMINDED`.
- `COMMAND` — `/start` and `/test` keep today's behaviour; `/help` is `HELP`.
- `CONTEXT` — the existing `_accept_context` path.
- `SMALL_TALK` — one warm line that names the current friend and restates the
  open question.
- `CANCEL` — marks the thread `ABANDONED`. A canned reply was considered and
  rejected: leaving the thread open means the next reminder still treats it as
  unfinished, which reads as the bot ignoring the user.
- `HELP`, `UNKNOWN` — a reply describing what is possible *right now*, given the
  thread's state.

The name-match rule also gives free-text friend switching, and makes a numbered
reply to a choice message survive a restart — `_last_options`
(`adapters/linq/webhook.py:28`) is in memory and will be empty by then.

## Case matrix

### When a reminder fires

| Situation | Behaviour |
|---|---|
| No other thread open | Plain reminder, as today. Thread created `REMINDED`, becomes active. |
| Another thread open (`REMINDED`…`SHOWING_GIFTS`) | Choice message listing each open friend with days-to-birthday. The new thread is created but not auto-activated; the active thread changes only when the user picks. |
| Open thread is `AWAITING_PAYMENT` | Choice message naming the pending checkout and warning the link expires in 15 minutes. Nothing is cancelled silently. |
| Third birthday while two are open | Choice lists all open threads sorted by nearest birthday, capped at three plus "…and N more". |
| Friend already `CONFIRMED` this cycle | Skipped, guarding against a re-fired reminder. |

The choice message is a normal `BotResponse.keyboard`, so Telegram renders
buttons and iMessage renders a numbered list. No new adapter code.

### Replies inside a thread

| State | Input | Behaviour |
|---|---|---|
| `REMINDED` | context text | Save, then budget or straight to picks |
| `REMINDED` | a budget | Store it, still ask for interests |
| `ASKING_BUDGET` | not a budget | If it reads as context, treat it as context; otherwise re-ask once, then fall back to the user's saved spend cap, or `$50` if they never set one |
| `SHOWING_GIFTS` | number / button | Checkout |
| `SHOWING_GIFTS` | "cheaper" / "anything else" | Re-run picks |
| `SHOWING_GIFTS` | stale button id | Re-show picks (existing behaviour) |
| `AWAITING_PAYMENT` | anything | "Still waiting" plus the link again; `CANCEL` abandons |
| `CONFIRMED` | anything | "Already sent", and offer the next open thread if one exists |
| any | small talk | Warm line naming the current friend, restating the open question |

### The ambiguous case

A choice message goes out and the user answers with context instead of picking
("she likes pottery"). Rather than blocking on "who do you mean?", it lands on
the **currently active** thread and says so:

> Got it — noting that for Adi. Say "Priya" to switch.

Never stalls, always recoverable, and the name rule makes the escape hatch real.

### Restart and payment

- The thread row survives, so the next message resumes at its own state. A bare
  `"2"` still resolves because `picks` is persisted.
- `AWAITING_PAYMENT` survives a restart but its polling task does not. On the
  next message in that state, poll Prava once before replying, so a payment that
  completed during the downtime is picked up instead of hanging forever.
- Checkout expiry, payment failure and Prava outage keep today's behaviour
  (`_retry`, the outage message), scoped to the thread.

## Out of scope

The same person on both Telegram and iMessage is two `users` rows with separate
threads. Wrong, invisible in a demo, and expensive to fix properly.

## Testing

`test_flow.py` is a plain assert-based script run with `python test_flow.py`;
new checks follow that pattern rather than introducing a framework.

- `core/intent.py` is a pure function — table-driven asserts over
  (text, state) → intent, with the Gemini call stubbed.
- Thread isolation: two open threads, reply to each, assert context lands on the
  right friend.
- The regression that motivates this: start a checkout for A, fire B's reminder,
  settle A's payment, assert the order is booked against **A**.
- Restart: write a thread in `SHOWING_GIFTS`, clear all in-memory state, reply
  `"2"`, assert the right product is chosen.
- Collision: two due reminders three days apart, assert the second produces a
  choice message and does not steal the active thread.
