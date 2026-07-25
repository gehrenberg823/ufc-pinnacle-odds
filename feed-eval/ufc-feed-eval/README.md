# UFC feed eval — Bolt play-by-play × Kalshi odds

A tiny standalone tool to judge whether **Bolt Odds'** live UFC data is good, by
showing it **side by side with Kalshi's live odds** moving, in real time.

Built for the **UFC card the weekend of July 25–26, 2026** (Ankalaev vs Guskov).
Everything is hardcoded — nothing to configure, no accounts, no keys to set up.

---

## Run it

Requires **Node 22 or newer** (it uses Node's built-in WebSocket). Check with:

```
node --version
```

**Windows:** double-click **`run.bat`** — it starts the server and opens your
browser. Leave the window open during the fights.

**Any OS / manual:**
```
node ufc-eval.mjs
```
then open **http://localhost:8899** in your browser.

> If you're on an older Node that says "no WebSocket", run `npm i ws` once in this
> folder, then run again.

---

## What you're looking at

**One card per fight** (all fights on the card). Each card has three Kalshi market
types plus the Bolt feed:

- **Winner** (the two big fighter cells) — each fighter's live implied win %, plus a
  color-coded **YES / NO** quote:
  - **YES** (green) = cost to bet that fighter **wins** (the ask)
  - **NO** (red) = cost to bet that fighter **loses** (100 − bid)
  - a sparkline of how the price has moved, and when it last moved.
- **Finish before** (round props) — Kalshi's "fight ends before round N" markets,
  each with its % and YES/NO price.
- **By method** (method-of-victory) — "Fighter wins by KO / Submission / Decision",
  each with its % and YES/NO price. Color bar shows which fighter (teal = left,
  amber = right).
- **Bolt play-by-play** (bottom strip) — the live fight events as Bolt streams them,
  timestamped. The card badge shows Bolt's connection state and how fresh it is.

**Live activity log** (top of page) — every odds move and every Bolt event as it
streams in, newest first. Filter it with the **all / kalshi / bolt** buttons.

**Header** — connection status for both feeds, how many fights Bolt is actually
feeding, and a clock.

### How to evaluate the data

When Bolt reports something happening in a fight (a knockdown, a finish, a round
ending), watch whether **Kalshi's odds move right after — and how fast**. If Bolt's
events consistently line up with, and *lead*, the market moves, the data is good.

---

## Things to know (so the numbers make sense)

- **Before markets are trading**, a fighter shows "— no market" and Bolt shows
  "subscribed — no plays yet." Both fill in once the card goes live.
- **Round and method markets are thin.** Their YES + NO often sum well over 100¢
  (e.g. 56 + 74) — that's a wide bid/ask spread on an illiquid book, not a bug.
  The liquid **winner** markets sum much closer to 100.
- **UFC only comes through Bolt's play-by-play feed**, not livescores — a fight has
  no running scoreboard to stream, so livescores doesn't carry it.
- **Bolt coverage is per-fight.** The header shows "X / N fights" — Bolt may not
  feed every bout on the card. Watching which ones it does feed is part of the eval.

---

## Everything is saved

Raw feeds are appended to **`./captures/`** as JSONL, so the whole session is on
disk for later review even if nobody's watching:

- `kalshi-ufc-<time>.jsonl` — every Kalshi price poll + timestamp
- `bolt-ufc-<time>.jsonl` — every Bolt message + the millisecond it arrived

---

## Notes

- The Bolt key inside `ufc-eval.mjs` is a **trial key that rotates end of Sunday** —
  hardcoded on purpose, nothing sensitive. If Bolt suddenly stops connecting after
  the weekend, that's why.
- Uses **only Kalshi's public API** — no Kalshi account or key needed.
- Fights and the Bolt ↔ Kalshi mapping are discovered automatically at startup from
  both APIs (matched by fighter last-name codes), so it just works for the card.
- If you want to tinker, see **`CLAUDE.md`** — it's a briefing for Claude Code on how
  the tool works and how to change it safely.
