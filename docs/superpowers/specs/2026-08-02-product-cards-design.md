# Product Cards Design

**Date:** 2026-08-02
**Scope:** How gift suggestions are presented in chat

## Problem

A suggestion is currently one line:

```
• *Vahdam Assorted Tea Sampler (10 teas)* ($24.00, Vahdam Teas) — she loves tea
```

No image, no link, nothing about the product. The user is asked to tap a button
and pay against a bare product name — so they mostly won't. Two smaller faults
sit inside the same line: the `*asterisks*` render literally because the
Telegram adapter never sets `parse_mode`, and `listing_url` is already captured
but never shown.

## What we already have

| Field | Source | Used today |
|---|---|---|
| name, price, merchant | Serper / catalog | yes |
| `listing_url` | Serper `link` | captured, never rendered |
| `image_url` | Serper `imageUrl` | **discarded in `_to_item`** |
| `reason` | Gemini | yes |

Most of the data exists. This is a presentation problem, not a sourcing one.

## Decisions

**One card per gift.** Three separate messages. Telegram cannot attach inline
buttons to a photo album, and a button per card is what makes the choice
unambiguous.

**Two lines of copy, not one.** A factual line about the product, then the
personal line tying it to the friend. The existing single line is only the
latter.

**Gemini is constrained to the title.** Serper returns no description, so the
model only ever sees a title, price and merchant. It may restate the title and
nothing more — no invented materials, quantities, colours, flavours or
features. Descriptions get plainer; they stop being able to be false. A user is
about to spend money against this text.

**The link is the Google Shopping listing**, not the derived merchant domain.
`merchant_url` is a best-effort guess that exists to satisfy Visa; `listing_url`
is the actual offer page a human should see.

**Telegram gets images; iMessage gets the same copy as text.** Linq supports
attachments, but the shape is unresearched and every unverified Linq field has
already cost us once. Parity is a follow-on, not a blocker.

## Core contract

`core/` must not learn what a photo is. It emits structured cards; each adapter
renders them its own way — the same boundary that let Linq reuse `core/`
untouched.

```python
@dataclass
class Card:
    title: str              # product name
    subtitle: str           # "$24.00 · Vahdam Teas"
    blurb: str              # what it is, constrained to the title
    reason: str             # why it suits this friend
    image_url: str | None
    link_url: str | None
    action_label: str       # "Choose this — $24.00"
    action_data: str        # "gift:live0_vahdam…"
```

`BotResponse` gains `cards: list[Card] | None`. `keyboard` stays for every other
prompt; cards do not replace it.

## Rendering

**Telegram** — one `send_photo` per card, caption carrying title, subtitle,
blurb, reason and link, with a single "Choose this" button beneath. `parse_mode`
is set, fixing the literal-asterisk bug.

**iMessage** — one numbered text block, same copy, `View:` links, "reply with a
number". Unchanged mechanics.

## Failure handling

Nothing here may cost the user a suggestion.

- **No image** (catalog items, or Serper omitted one) → text card, same content.
- **Photo send fails** (gstatic URLs expire, network) → fall back to a text card
  rather than dropping the product.
- **Gemini omits `blurb`** → render the card without that line.
- **Missing `listing_url`** → no link line, card still sends.

## Tests

- A card is built correctly from a live product and from a catalog item
- Cards without an image still carry title, price, blurb and action
- The blurb constraint is present in the prompt
- Photo failure produces a text card with the same content, not a dropped card
- `action_data` stays within Telegram's 64-byte `callback_data` limit
- Numbered iMessage replies still resolve to the right product

## Out of scope

- Images on iMessage — follow-on, adapter-only, contract already supports it
- Product descriptions from a real catalogue source; requires a vendor API
- Carousels, pagination, "show me more"

## Consequence

Three messages per suggestion instead of one. On Telegram that is free. On
iMessage the sandbox caps at 100 messages/day, which a text block does not
meaningfully change; it would matter if images land there later.
