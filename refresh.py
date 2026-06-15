#!/usr/bin/env python3
"""Scrape Pinnacle's public guest API for the next UFC card, de-vig every fight's
markets, and render a self-contained HTML dashboard (index.html).

Markets produced per fight (all de-vigged to fair probabilities):
  * Moneyline           - 2-way (fighter A vs fighter B)
  * Method of victory   - fighter x {KO/TKO, Submission, Decision}, field-normalized
  * Method of finish    - aggregate {KO/TKO, Submission, Decision} across both fighters
  * Go the distance     - "Fight Goes To Decision" Yes/No, 2-way
  * Round of finish     - which round the fight ends (or Decision), built from the
                          "Fight Starts Round N" ladder + go-the-distance

NOTE: Pinnacle does NOT price "round of victory" (a specific fighter winning in a
specific round), so that market is intentionally omitted.

De-vig methodology:
  * 2-way markets  -> Yes/A implied vs No/B implied, normalized to sum 1.
  * field markets  -> normalize implied probability across the whole field.
  * round-of-finish-> reach[1]=1, reach[n]=P(starts round n); the telescoping
    differences (reach[n]-reach[n+1]) plus go-the-distance form a distribution
    that sums to 1 by construction.

Usage:
    python3 refresh.py            # fetch live + (re)write index.html
    python3 refresh.py --date 2026-06-15   # force a specific card date (UTC)
"""
from __future__ import annotations
import argparse, html, json, os, re, sys, time
from collections import defaultdict
import requests

KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"          # Pinnacle public guest x-api-key
SPORT = 22                                         # Mixed Martial Arts
LEAGUE = 1624                                      # UFC
BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
HDR = {"x-api-key": KEY, "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def implied(american: float) -> float:
    """American odds -> raw implied probability (still vigged)."""
    a = float(american)
    return (-a) / (-a + 100.0) if a < 0 else 100.0 / (a + 100.0)


# ---------------------------------------------------------------- fetch -------
def fetch():
    mu = requests.get(f"{BASE}/leagues/{LEAGUE}/matchups",
                      headers=HDR, params={"brandId": 0}, timeout=40).json()
    straight = requests.get(f"{BASE}/leagues/{LEAGUE}/markets/straight",
                            headers=HDR, timeout=60).json()
    # moneyline prices per matchup. Two shapes:
    #   - real fights: priced by designation (home/away), participantId == None
    #   - prop markets: priced by participantId (Yes/No), designation == None
    by_design = {}   # matchupId -> {home: american, away: american}
    by_part = {}     # matchupId -> {participantId: american}
    for mk in straight:
        if mk.get("key") != "s;0;m":
            continue
        mid = mk["matchupId"]
        for p in mk.get("prices", []):
            if p.get("participantId") is not None:
                by_part.setdefault(mid, {})[p["participantId"]] = p["price"]
            elif p.get("designation"):
                by_design.setdefault(mid, {})[p["designation"]] = p["price"]
    return mu, by_design, by_part


# --------------------------------------------------------------- helpers ------
def devig_two(a_yes, a_no):
    """Return fair P(yes) from a 2-way (yes/no) american pair, or None."""
    if a_yes is None or a_no is None:
        return None
    iy, ino = implied(a_yes), implied(a_no)
    tot = iy + ino
    return iy / tot if tot > 0 else None


def yes_no_ids(part_list):
    d = {p["name"]: p["id"] for p in part_list}
    return d.get("Yes"), d.get("No")


# Method strings as they appear in Pinnacle descriptions -> canonical label.
METHODS = [("TKO/KO", "KO/TKO"), ("Submission", "Submission"), ("Decision", "Decision")]


def build_card(mu, by_design, by_part, date_filter=None):
    byid = {m["id"]: m for m in mu}
    children = defaultdict(list)
    fights = []
    for m in mu:
        if m.get("special") or m.get("parentId"):
            if m.get("parentId"):
                children[m["parentId"]].append(m)
            continue
        if m.get("type") == "matchup" and len(m.get("participants", [])) == 2:
            fights.append(m)

    # pick the card: soonest date that has fights, unless overridden
    dates = sorted({(m.get("startTime") or "")[:10] for m in fights if m.get("startTime")})
    card_date = date_filter or (dates[0] if dates else None)
    fights = [m for m in fights if (m.get("startTime") or "").startswith(card_date or "")]
    # main event last in startTime -> show latest first
    fights.sort(key=lambda m: m.get("startTime", ""), reverse=True)

    out = []
    for f in fights:
        fid = f["id"]
        parts = {p["alignment"]: p["name"] for p in f["participants"]}
        home, away = parts.get("home"), parts.get("away")
        fight = {"id": fid, "start": f.get("startTime"),
                 "home": home, "away": away, "markets": {}}

        # ---- moneyline (devig home/away) ----
        d = by_design.get(fid, {})
        p_home = devig_two(d.get("home"), d.get("away"))
        if p_home is not None:
            fight["markets"]["moneyline"] = {home: p_home, away: 1 - p_home}

        # index this fight's prop children by description
        props = {}
        for c in children.get(fid, []):
            desc = (c.get("special") or {}).get("description")
            if desc:
                props[desc] = c

        def yes_implied(desc):
            """Raw (vigged) implied P(yes) for a named Yes/No prop, or None."""
            c = props.get(desc)
            if not c:
                return None
            yid, _ = yes_no_ids(c["participants"])
            a = by_part.get(c["id"], {}).get(yid)
            return implied(a) if a is not None else None

        def gtd():
            c = props.get("Fight Goes To Decision")
            if not c:
                return None
            yid, nid = yes_no_ids(c["participants"])
            pr = by_part.get(c["id"], {})
            return devig_two(pr.get(yid), pr.get(nid))

        # ---- method of victory (field-normalize the per-fighter method legs) ----
        legs = {}   # (fighter, method_label) -> raw implied
        for fighter in (home, away):
            for needle, label in METHODS:
                imp = yes_implied(f"{fighter} To Win By {needle}")
                if imp is not None:
                    legs[(fighter, label)] = imp
        if legs:
            tot = sum(legs.values())
            mov = {f"{k[0]} - {k[1]}": v / tot for k, v in legs.items()} if tot > 0 else {}
            fight["markets"]["method_of_victory"] = mov
            # ---- method of finish (aggregate methods across both fighters) ----
            agg = defaultdict(float)
            for (_, label), v in legs.items():
                agg[label] += v / tot if tot > 0 else 0
            fight["markets"]["method_of_finish"] = dict(agg)

        # ---- go the distance ----
        g = gtd()
        if g is not None:
            fight["markets"]["go_the_distance"] = {"Goes to decision": g,
                                                   "Ends inside distance": 1 - g}

        # ---- round of finish (built from the "starts round N" ladder) ----
        reach = {1: 1.0}
        n = 2
        while True:
            c = props.get(f"Fight Starts Round {n}")
            if not c:
                break
            yid, nid = yes_no_ids(c["participants"])
            pr = by_part.get(c["id"], {})
            r = devig_two(pr.get(yid), pr.get(nid))
            if r is None:
                break
            reach[n] = r
            n += 1
        last = max(reach)
        if last >= 2 and g is not None:
            rof, prev = {}, None
            for k in range(1, last + 1):
                nxt = reach.get(k + 1, g)   # after the final priced round, the rest is "decision"
                rof[f"Round {k}"] = max(0.0, reach[k] - nxt)
            rof["Decision"] = g
            s = sum(rof.values())
            if s > 0:
                rof = {k: v / s for k, v in rof.items()}   # renormalize after clamping
            fight["markets"]["round_of_finish"] = rof

        out.append(fight)
    return card_date, out


# ----------------------------------------------------------------- render -----
MARKET_TITLES = [
    ("moneyline", "Moneyline"),
    ("method_of_victory", "Method of Victory"),
    ("go_the_distance", "Go the Distance"),
    ("round_of_finish", "Round of Finish"),
]


def pct(v):
    return f"{v * 100:.1f}%"


def render(card_date, fights, fetched):
    esc = html.escape
    cards = []
    for f in fights:
        tbls = []
        for key, title in MARKET_TITLES:
            mk = f["markets"].get(key)
            if not mk:
                continue
            rows = sorted(mk.items(), key=lambda kv: -kv[1])
            body = "".join(
                f'<tr><td>{esc(str(k))}</td><td class="p">{pct(v)}</td>'
                f'<td class="bar"><span style="width:{max(1, v*100):.1f}%"></span></td></tr>'
                for k, v in rows)
            tbls.append(f'<div class="mkt"><h3>{esc(title)}</h3>'
                        f'<table><tbody>{body}</tbody></table></div>')
        st = (f.get("start") or "").replace("T", " ").replace("Z", " UTC")
        cards.append(
            f'<section class="fight"><header><h2>{esc(f["away"])} '
            f'<span class="vs">vs</span> {esc(f["home"])}</h2>'
            f'<span class="time">{esc(st)}</span></header>'
            f'<div class="mkts">{"".join(tbls)}</div></section>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UFC — Pinnacle De-vigged Fair Odds</title>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--line:#21262d;--txt:#e6edf3;--mut:#8b949e;--accent:#d20a0a}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
  .wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 60px}}
  h1{{font-size:26px;margin:0 0 4px}}
  .sub{{color:var(--mut);margin:0 0 26px;font-size:13px}}
  .sub b{{color:var(--txt)}}
  .fight{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 0 22px;overflow:hidden}}
  .fight>header{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
    padding:14px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}}
  .fight h2{{font-size:18px;margin:0}}
  .vs{{color:var(--mut);font-weight:400;font-size:13px;padding:0 4px}}
  .time{{color:var(--mut);font-size:12px;white-space:nowrap}}
  .mkts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:0}}
  .mkt{{padding:14px 18px;border-top:1px solid var(--line);border-right:1px solid var(--line)}}
  .mkt h3{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 8px}}
  table{{width:100%;border-collapse:collapse}}
  td{{padding:3px 0;vertical-align:middle;font-size:13.5px}}
  td.p{{text-align:right;font-variant-numeric:tabular-nums;width:58px;padding-right:10px}}
  td.bar{{width:34%}}
  td.bar span{{display:block;height:7px;border-radius:4px;background:var(--accent);opacity:.85}}
  footer{{color:var(--mut);font-size:12px;margin-top:30px;text-align:center}}
  a{{color:#58a6ff}}
</style></head>
<body><div class="wrap">
<h1>UFC — Pinnacle De-vigged Fair Odds</h1>
<p class="sub">Card date <b>{esc(card_date or "?")}</b> (UTC) · {len(fights)} fights ·
   source <b>Pinnacle</b> · de-vigged · fetched <b>{esc(fetched)}</b><br>
   Round of <i>victory</i> omitted (not priced by Pinnacle). Round of <i>finish</i> = which round it ends, either fighter.</p>
{"".join(cards)}
<footer>Probabilities are vig-removed and normalized within each market. Built from Pinnacle guest API league {LEAGUE}.</footer>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="force card date YYYY-MM-DD (UTC)")
    ap.add_argument("--json", action="store_true", help="also dump raw probabilities to stdout")
    args = ap.parse_args()

    print("Fetching Pinnacle UFC matchups + prices ...")
    mu, by_design, by_part = fetch()
    card_date, fights = build_card(mu, by_design, by_part, args.date)
    print(f"Card {card_date}: {len(fights)} fights")
    for f in fights:
        got = ", ".join(k for k, _ in MARKET_TITLES if k in f["markets"])
        print(f"  {f['away']} vs {f['home']:30s} [{got}]")
    fetched = time.strftime("%Y-%m-%d %H:%M %Z")
    open(OUT, "w", encoding="utf-8").write(render(card_date, fights, fetched))
    print(f"\nWrote {OUT}")
    if args.json:
        print(json.dumps(fights, indent=2))
