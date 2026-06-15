# UFC — Pinnacle De-vigged Fair Odds

Scrapes Pinnacle's public guest API for the next UFC card, removes the vig from
every market, and renders a self-contained HTML dashboard (`index.html`) with
one card per fight.

**Live page:** https://gehrenberg823.github.io/ufc-pinnacle-odds/

## Markets (all vig-removed, normalized to sum to 1)

| Market | Source on Pinnacle |
|---|---|
| **Moneyline** | matchup money line, 2-way |
| **Method of Victory** | fighter × {KO/TKO, Submission, Decision}, field-normalized |
| **Method of Finish** | aggregate {KO/TKO, Submission, Decision} across both fighters |
| **Go the Distance** | "Fight Goes To Decision" Yes/No |
| **Round of Finish** | "Fight Starts Round N" ladder + go-the-distance (handles 3- and 5-round fights) |

> **Round of *victory*** (a specific fighter winning in a specific round) is omitted
> because Pinnacle does not price it.

## Usage

```bash
pip install -r requirements.txt
python3 refresh.py                    # fetch live + (re)write index.html (auto-picks next card)
python3 refresh.py --date 2026-06-20  # force a specific card date (UTC)
python3 refresh.py --json             # also dump raw probabilities
```

## De-vig methodology

- **2-way markets** — Yes/A implied vs No/B implied, normalized to sum 1.
- **Field markets** (method of victory) — normalize implied probability across the whole field.
- **Round of finish** — `reach[1]=1`, `reach[n]=P(starts round n)`; the telescoping
  differences `reach[n]-reach[n+1]` plus go-the-distance form a distribution that
  sums to 1 by construction.

Probabilities are derived from the Pinnacle guest API (MMA league 1624). This is
an analytical tool, not affiliated with Pinnacle or the UFC.
