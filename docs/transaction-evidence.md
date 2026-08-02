# Prava Transaction Evidence

Verifiable record of GiftBot completing a real Prava sandbox transaction, plus
an honest account of what is and is not wired up.

## The completed transaction

Sandbox, 2026-08-01 ~22:50 IST. A user picked a gift in Telegram, approved on
Prava's hosted page with a passkey, and the session settled.

```
session_id   ses_01KYZ276Q2H0R84KS48AZS8R94
order_id     ord_01KYZ276Q2H0R84KS48AZS8R94
txn_id       txn_01KYZ29MFAS6YGSYQWND16PH30
txn_ref_id   tli_01KYZ29MFFAP4JG0E1559EJH8V

session status        completed
transaction status    completed
line item status      completed
visa_confirmation     SUCCESS

merchant     GiftBot Demo Store  (https://example.com)
product      Cozy Reading Kit
amount       25.00 USD
credentials  one-time token issued, expiry 12/2030
```

Re-confirmed against Prava on 2026-08-02 10:52 IST, still `completed`
(`X-Response-ID: 15264ac4-b40a-4f24-84d9-c29db59fc14a`).

### Verify it yourself

```bash
curl https://sandbox.api.prava.space/v1/sessions/ses_01KYZ276Q2H0R84KS48AZS8R94/payment-result \
  -H "Authorization: Bearer sk_test_..."
```

## The flow that produced it

Prava does not charge a merchant on your behalf. It issues a **single-use,
merchant-locked, amount-scoped virtual card** after the human approves with a
passkey. Four steps:

| # | Call | Result |
|---|------|--------|
| 1 | `POST /v1/sessions` | `session_id` + hosted `iframe_url`, valid 15 min |
| 2 | *human* opens the page | enters card, OTP, approves with Face ID / Touch ID |
| 3 | `GET /v1/sessions/{id}/payment-result` | polled until `awaiting_result`; returns `token` + `dynamic_cvv` |
| 4 | `POST /v1/sessions/{id}/report-status` | reports the outcome; session becomes `completed` |

Creating a session alone is not a completed order. Step 4 is what settles it,
and it is what returned `visa_confirmation: SUCCESS` above.

**Polling, not webhooks.** Prava's webhook delivery has not shipped — their docs
say *"Coming soon: configuration exists today, delivery is rolling out"* and
direct you to poll. So GiftBot polls, and needs no public callback endpoint.

## What is real, and what is not

| Layer | Status |
|---|---|
| Product discovery | **Real** — live Google Shopping results, real merchants and prices |
| Gift selection | **Real** — Gemini choosing from those results, constrained to real ids |
| Conversation | **Real** — Telegram and iMessage (via Linq), one shared core |
| Reminders | **Real** — scheduler messages the user unprompted before a birthday |
| Payment | **Real** — Prava sandbox, passkey-approved, network-confirmed |
| Merchant fulfilment | **Not wired** — see below |

The Prava transaction is genuine. What does not happen is the **downstream
merchant purchase**: the issued card is never presented at the merchant's own
checkout, so no gift is actually shipped. `report-status` reports the sandbox
outcome only, and the bot says so in chat rather than implying an order was
placed:

> ✅ Prava sandbox payment completed — test mode
> *Sandbox transaction — no real funds moved and no merchant order was placed.*

Closing that gap needs UCP (agent-driven checkout at participating merchants).
Prava support confirmed UCP has no sandbox host and runs against live merchants
only, so it was out of scope for a sandbox build.

## Incident log

Sandbox reliability during the build, kept because it shaped the engineering.

| Time (IST) | Observation |
|---|---|
| 01 Aug 22:50 | Full checkout succeeds, `visa_confirmation: SUCCESS` |
| 01 Aug 23:00 | Every endpoint returns 504 after ~61s, including calls that worked minutes earlier |
| 01 Aug 23:25 | Flaps to `401 AUTH_1001` after ~59s. A deliberately fabricated key returned the same 504, proving requests never reached auth — platform-level, not account-level |
| 02 Aug 08:41 | Still failing ~9.5h later |
| 02 Aug 08:48 | Recovered after Prava reissued the API key: `201` in 1.5s |
| 02 Aug 10:33 | Checkout fails at `FETCH_AGENTIC_CREDS_ERROR` — "Visa 400, Fetching cryptogram failed" |
| 02 Aug 10:44 | Same error with a verified real merchant domain, ruling out the merchant URL |
| 02 Aug ~10:50 | Prava confirmed the **team test card was exhausted**; card exhaustion surfaces as that cryptogram error, not as anything card-related |

This drove the resilience work: retries on transient 5xx only, polling that
survives a mid-checkout outage, harder retries on `report-status` (losing it
strands a paid session), and a user-facing message that says nothing was
charged rather than failing ambiguously.
