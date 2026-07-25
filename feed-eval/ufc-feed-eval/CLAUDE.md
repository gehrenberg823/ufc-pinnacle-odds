# CLAUDE.md — briefing for Claude Code working on this tool

This is a **single-file, standalone** eval tool. It has nothing to do with any larger
repo — don't look for one. The whole program is **`ufc-eval.mjs`** (plain Node ESM,
no build step, no dependencies beyond Node 22+).

## What it does

Runs a local web dashboard (http://localhost:8899) that shows **Bolt Odds' live UFC
play-by-play** side by side with **Kalshi's live UFC odds**, so a human can judge
whether Bolt's UFC data is good enough to buy. It is a throwaway evaluation harness
for one weekend's fight card, not production code. Favor clarity and "it just works
when double-clicked" over abstraction.

## How to run / restart

```
node ufc-eval.mjs          # foreground
```
On Windows, to restart it detached (so it survives your shell exiting):
```powershell
# kill whatever is on 8899, then relaunch hidden
Get-NetTCPConnection -LocalPort 8899 -State Listen -EA SilentlyContinue |
  %{ Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }
Start-Process node -ArgumentList "ufc-eval.mjs" -WorkingDirectory (Get-Location) `
  -WindowStyle Hidden -RedirectStandardOutput server.log -RedirectStandardError server.err
```
Verify: `curl http://localhost:8899/stream` should emit a `data: {…}` SSE line with
`kalshiOk:true`. After a restart, give the paced scheduler **~25s** to price the
thin prop markets before judging whether prices are "missing."

## Architecture (one file, top to bottom)

- **Config block** — hardcoded Bolt trial key, Kalshi base URL, Kalshi series tickers,
  port, pacing. The Bolt key **rotates end of Sunday**; if Bolt stops connecting after
  the weekend, the key is dead — that's expected, not a bug to chase.
- **`discover()`** — pulls Kalshi `KXUFCFIGHT` markets, picks the **soonest** card by
  date token, builds one fight per event (two fighter markets each), then maps Bolt
  games to fights by **fighter last-3-letter codes**.
- **`fetchProps()`** — discovers `KXUFCROUNDS` (round props) and `KXUFCMOV`
  (method-of-victory) markets and attaches them to fights. Re-runs every 60s to pick
  up markets Kalshi opens closer to the event.
- **Paced price scheduler** (`rebuildTargets` / `priceTick`) — see the rate-limit note
  below. This is the heart of the Kalshi side.
- **Bolt WebSocket** (`connectBolt` / `handleBolt` / `summarizeBolt`) — subscribes to
  the play-by-play feed and folds messages into each fight.
- **SSE `/stream` + inline HTML/JS dashboard** — server pushes a JSON snapshot; the
  browser renders cards. All the client code is one `<script>` at the bottom.

## Hard-won gotchas — READ BEFORE CHANGING ANYTHING

These are real bugs we already hit and fixed. Don't reintroduce them.

1. **Kalshi rate-limits bursts (HTTP 429).** Do **not** loop over all markets firing
   orderbook requests at once. The scheduler deliberately sends **one orderbook
   request every ~200ms**, cycling all markets (winners weighted 3× for freshness),
   and backs off on 429. Keep that shape.
2. **Kalshi's market *summary* has null bid/ask/last before trading.** Live odds live
   in the **orderbook** — price off `orderbook_fp` (`yes_dollars` / `no_dollars`),
   see `obBidAsk()`. Prices are in **cents = %**.
3. **YES/NO framing:** `YES = ask` (cost to buy the outcome), `NO = 100 − bid` (cost
   to fade it). Used identically for winner, round, and method markets.
4. **Method-of-victory markets use a non-`open` status** — querying `?status=open`
   returns zero. Fetch the series **without** a status filter (we missed ~91 markets
   this way).
5. **Node's built-in WebSocket delivers frames as `Blob`.** You must
   `await e.data.text()` to read them — `String(e.data)` yields `"[object Blob]"` and
   silently drops every Bolt message. See the `onmessage` handler.
6. **UFC is play-by-play only on Bolt.** The livescores feed rejects UFC (no running
   scoreboard exists for a fight). Don't try to "fix" the Bolt side by switching feeds.
7. **Bolt game strings are `"Fighter A vs Fighter B, DATE, id"`.** Split off `,` FIRST,
   then ` vs `, or the date/id contaminates the second fighter's code. A fight can map
   to **multiple** Bolt ids; we keep all of them so one bad id can't blank a fight.

## Known open question (this is the point of the eval)

**We do not yet know how — or whether — Bolt cleanly signals "end of round"** for UFC,
because we've never captured a live fight. The tool reads `round`, `status`, `clock`,
and `result` from Bolt's MMA `play_info`. The end-of-round signal will be one of:
the `round` number ticking up, the `clock` hitting 0:00, or a `status` flip. Watch the
raw `captures/bolt-ufc-*.jsonl` during the fights to confirm which. If the human asks
you to make end-of-round detection explicit, add a watcher that flags when `round`
increments or `clock` reaches 0, and log it prominently — don't guess the schema,
confirm it against the capture files.

## Data captured for later review

Everything both feeds send is appended to `./captures/*.jsonl` with arrival
timestamps. These files are the ground truth for any latency or schema analysis —
prefer reading them over re-deriving behavior.

## Style

Keep it a single dependency-free file. Small readable functions. No frameworks, no
bundler, no TypeScript. If a change needs a package, reconsider — the whole value of
this tool is that a coworker can unzip it and run one command.
