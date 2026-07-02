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
import argparse, html, json, os, re, sys, time, unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
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


# ============================================================== KALSHI =========
# Kalshi has UFC moneyline only (KXUFCFIGHT, one event per fight, 2 markets).
# Prices live in the order book, not the summary bid/ask.
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _kget(path, tries=6):
    """GET Kalshi JSON with backoff on rate-limit (429) — bursts get throttled."""
    delay = 0.4
    for _ in range(tries):
        try:
            r = requests.get(KALSHI + path, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if r.status_code == 429:
                time.sleep(delay); delay *= 2; continue
            return r.json()
        except Exception:
            time.sleep(delay); delay *= 2
    return {}


def norm_fighter(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z]", "", s)


_NAME_NOISE = {"de", "da", "do", "dos", "del", "la", "le", "van", "von", "jr", "sr", "bra"}


def fighter_tokens(name: str):
    """Meaningful name tokens (deaccented, lowercased), dropping connectors/country tags."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return [t for t in re.findall(r"[a-z]+", s) if t not in _NAME_NOISE and len(t) >= 2]


def fighter_match(a, b) -> bool:
    """Fuzzy match two fighter names (token lists): handles nicknames, country tags,
    and extra surnames. Match if they share >=2 tokens, or same surname + first initial."""
    sa, sb = set(a), set(b)
    if len(sa & sb) >= 2:
        return True
    return bool(a and b and a[-1] == b[-1] and a[0][:1] == b[0][:1])


def card_date_code(card_date: str) -> str:
    """'2026-06-20' -> '26JUN20' (Kalshi event-ticker date code)."""
    y, m, dd = card_date.split("-")
    return f"{y[2:]}{_MONTHS[int(m) - 1]}{dd}"


def kalshi_mid(ticker: str):
    """Kalshi YES implied prob from the order book (mid of best bid / derived ask)."""
    ob = _kget(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}
    yes, no = ob.get("yes_dollars") or [], ob.get("no_dollars") or []
    ybid = max((float(p) for p, _ in yes), default=None)
    yask = (1.0 - max(float(p) for p, _ in no)) if no else None
    if ybid is not None and yask is not None:
        return (ybid + yask) / 2.0
    return ybid if ybid is not None else yask


def fetch_kalshi_ufc(card_date: str):
    """Return [{fighters: {norm_name:(display,ticker)}, url}] for the card's KXUFCFIGHT
    events. Discovery only (events + markets calls) — no order-book calls here, so price
    throttling can't drop a match."""
    code = card_date_code(card_date)
    evs = _kget("/events?series_ticker=KXUFCFIGHT&status=open&limit=100").get("events", [])
    out = []
    for e in evs:
        et = e["event_ticker"]
        if code not in et:
            continue
        mk = _kget(f"/markets?event_ticker={et}&limit=10").get("markets", [])
        fighters = {}
        for m in mk:
            sub = m.get("yes_sub_title")
            if sub:
                fighters[norm_fighter(sub)] = (sub, m["ticker"])
        if fighters:
            out.append({"fighters": fighters, "url": f"https://kalshi.com/markets/kxufcfight/{et.lower()}"})
    return out


def _match_side(toks, cand):
    """Pick the Kalshi fighter for one Pinnacle fighter. Falls back to a
    unique-surname match for ring-name diffs (Kalshi 'Bobby Green' vs
    Pinnacle 'King Green') — safe inside a two-fighter event."""
    hit = next((c for c in cand if fighter_match(toks, c[0])), None)
    if hit:
        return hit
    sn = toks[-1] if toks else ""
    hits = [c for c in cand if c[0] and c[0][-1] == sn]
    return hits[0] if len(hits) == 1 else None


def attach_kalshi(fights, card_date):
    """Match each fight to its Kalshi event (fuzzy fighter names, then unique
    surname), then price the matched markets."""
    kev = fetch_kalshi_ufc(card_date)
    for f in fights:
        ht, at = fighter_tokens(f["home"]), fighter_tokens(f["away"])
        for e in kev:
            cand = [(fighter_tokens(disp), tkr) for disp, tkr in e["fighters"].values()]
            hm, am = _match_side(ht, cand), _match_side(at, cand)
            if hm and am and hm[1] != am[1]:
                f["kalshi"] = {f["home"]: kalshi_mid(hm[1]), f["away"]: kalshi_mid(am[1])}
                f["kalshi_url"] = e["url"]
                break


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

    # pick the card. A UFC card runs ~5-6h continuously but can cross midnight UTC,
    # so a single card may span two UTC calendar dates. Cluster by time gaps instead
    # of by date: the soonest card = the earliest fight + all contiguous fights with
    # <12h gaps (the next card is days/weeks later).
    def _parse(m):
        return datetime.fromisoformat(m["startTime"].replace("Z", "+00:00"))

    fights = sorted((m for m in fights if m.get("startTime")), key=lambda m: m["startTime"])
    if date_filter:
        fights = [m for m in fights if m["startTime"].startswith(date_filter)]
        card_date = date_filter
    elif fights:
        card = [fights[0]]
        for m in fights[1:]:
            if _parse(m) - _parse(card[-1]) <= timedelta(hours=12):
                card.append(m)
            else:
                break
        fights = card
        # label the card by the local date of its earliest fight (US card date)
        card_date = _parse(fights[0]).astimezone().strftime("%Y-%m-%d")
    else:
        card_date = None
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


def fmt_start(s):
    """ISO start time -> readable local 12-hour time, e.g. 'Sat Jun 20 · 4:00 PM CDT'."""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().strftime(
            "%a %b %-d · %-I:%M %p %Z")
    except Exception:
        return s[:16].replace("T", " ")


def render(card_date, fights, fetched):
    esc = html.escape
    cards, chips = [], []
    for i, f in enumerate(fights, 1):
        tbls = []
        for key, title in MARKET_TITLES:
            mk = f["markets"].get(key)
            if not mk:
                continue
            rows = sorted(mk.items(), key=lambda kv: -kv[1])
            kal = f.get("kalshi") if key == "moneyline" else None
            if kal:
                # Moneyline with Kalshi comparison: Fair / Kalshi / Edge
                body = ""
                for k, v in rows:
                    kv = kal.get(k)
                    if kv is not None:
                        edge = (v - kv) * 100
                        ecls = "pos" if edge > 0 else ("neg" if edge < 0 else "")
                        kcell = pct(kv)
                        ecell = f'<td class="e"><span class="epill {ecls}">{edge:+.1f}</span></td>'
                    else:
                        kcell, ecell = "—", '<td class="e"></td>'
                    body += (f'<tr><td>{esc(str(k))}</td><td class="p">{pct(v)}</td>'
                             f'<td class="k">{kcell}</td>{ecell}</tr>')
                ttl = (f'<a href="{esc(f["kalshi_url"])}" target="_blank" rel="noopener">{esc(title)} &#8599;</a>'
                       if f.get("kalshi_url") else esc(title))
                tbls.append(f'<div class="mkt"><h3>{ttl}</h3><table>'
                            f'<thead><tr><th></th><th>Fair</th><th>Kalshi</th><th>Edge</th></tr></thead>'
                            f'<tbody>{body}</tbody></table></div>')
            else:
                body = "".join(
                    f'<tr><td>{esc(str(k))}</td><td class="p">{pct(v)}</td>'
                    f'<td class="bar"><span style="width:{max(1, v*100):.1f}%"></span></td></tr>'
                    for k, v in rows)
                nok = ('<span class="nok">no Kalshi market</span>'
                       if key == "moneyline" else '')
                tbls.append(f'<div class="mkt"><h3>{esc(title)} {nok}</h3>'
                            f'<table><tbody>{body}</tbody></table></div>')
        st = fmt_start(f.get("start"))
        chips.append(f'<a class="chip" href="#f{i}">{esc(f["away"])} vs {esc(f["home"])}</a>')
        cards.append(
            f'<section class="fight" id="f{i}"><header>'
            f'<h2>{esc(f["away"])} <span class="vs">vs</span> {esc(f["home"])}</h2>'
            f'<span class="time">{esc(st)}</span></header>'
            f'<div class="mkts">{"".join(tbls)}</div></section>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UFC Fair Odds</title>
<style>
  :root{{--bg:#0b0e14;--card:#12161f;--card2:#171c27;--line:#232a38;--txt:#dfe6f0;
    --mut:#7d8794;--blue:#4d9fff;--green:#2ea45f;--greenT:#3fb950;--red:#f85149}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--txt);
    font:14px/1.45 -apple-system,"SF Pro Text",Segoe UI,Roboto,sans-serif}}
  .num{{font-variant-numeric:tabular-nums}}

  /* sticky header (navslot/rbslot are filled by the local server only) */
  .tophdr{{position:sticky;top:0;z-index:50;background:rgba(11,14,20,.92);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}}
  .hrow{{display:flex;align-items:center;gap:18px;padding:12px 22px;max-width:1180px;margin:0 auto}}
  .brand{{font-size:15px;font-weight:700;white-space:nowrap}}
  .brand small{{color:var(--mut);font-weight:500;margin-left:8px}}
  .tabs{{display:flex;gap:2px;background:var(--card);border:1px solid var(--line);
    border-radius:8px;padding:2px}}
  .tabs a{{color:var(--mut);text-decoration:none;padding:5px 14px;border-radius:6px;font-size:12.5px}}
  .tabs a.on{{color:var(--txt);background:var(--card2);font-weight:600}}
  .spacer{{flex:1}}
  .stamp{{color:var(--mut);font-size:11.5px;text-align:right;line-height:1.5}}
  button.ghost{{background:var(--card);border:1px solid var(--line);color:var(--txt);
    border-radius:7px;padding:7px 14px;cursor:pointer;font-size:12.5px;font-weight:500}}
  button.ghost:hover{{filter:brightness(1.2)}} button.ghost:disabled{{opacity:.6;cursor:default}}

  .wrap{{max-width:1180px;margin:0 auto;padding:18px 22px 60px}}
  .chips{{display:flex;gap:8px;overflow-x:auto;padding:2px 0 14px;scrollbar-width:thin}}
  .chip{{white-space:nowrap;color:var(--txt);text-decoration:none;font-size:12.5px;
    padding:6px 12px;border-radius:999px;background:var(--card);border:1px solid var(--line)}}
  .chip:hover{{border-color:var(--blue)}}

  .fight{{background:var(--card);border:1px solid var(--line);border-radius:12px;
    margin:0 0 14px;overflow:hidden}}
  .fight>header{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
    padding:13px 18px;flex-wrap:wrap}}
  .fight h2{{font-size:15px;font-weight:650;margin:0}}
  .vs{{color:var(--mut);font-weight:400;font-size:12px;padding:0 4px}}
  .time{{color:var(--mut);font-size:11.5px;white-space:nowrap}}
  .mkts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;
    padding:0 18px 16px}}
  .mkt{{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
  .mkt h3{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--blue);
    font-weight:650;margin:0 0 8px}}
  .mkt h3 a{{color:var(--blue);text-decoration:none}} .mkt h3 a:hover{{text-decoration:underline}}
  .nok{{color:var(--mut);font-weight:500;text-transform:none;letter-spacing:0;margin-left:6px}}
  table{{width:100%;border-collapse:collapse}}
  td{{padding:4px 0;vertical-align:middle;font-size:13px;border-bottom:1px solid #1b2130}}
  tr:last-child td{{border-bottom:0}}
  td.p{{text-align:right;font-variant-numeric:tabular-nums;width:56px;padding-right:12px}}
  td.bar{{width:38%}}
  td.bar span{{display:block;height:6px;border-radius:3px;
    background:linear-gradient(90deg,#2b6cb0,var(--blue))}}
  th{{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
    text-align:right;font-weight:500;padding:0 12px 5px 0;border-bottom:1px solid var(--line)}}
  th:first-child{{text-align:left}}
  td.k{{text-align:right;font-variant-numeric:tabular-nums;width:56px;padding-right:12px;color:var(--mut)}}
  td.e{{text-align:right;width:58px}}
  .epill{{display:inline-block;min-width:44px;text-align:center;font-size:11px;font-weight:700;
    padding:2px 7px;border-radius:999px;font-variant-numeric:tabular-nums;
    background:#1b2130;color:var(--mut)}}
  .epill.pos{{background:#12351f;color:var(--greenT)}}
  .epill.neg{{background:#3a1513;color:var(--red)}}
  .legend{{color:var(--mut);font-size:11.5px;margin-top:18px;line-height:1.6}}
  .legend b{{color:var(--txt)}}
  a{{color:var(--blue)}}
</style></head>
<body>
<div class="tophdr"><div class="hrow">
  <span class="brand">UFC Fair Odds<small>{esc(card_date or "?")} · {len(fights)} fights</small></span>
  <span id="navslot"></span>
  <span class="spacer"></span>
  <span class="stamp">Pinnacle de-vigged · fetched {esc(fetched)}</span>
  <span id="rbslot"></span>
</div></div>
<div class="wrap">
<div class="chips">{"".join(chips)}</div>
{"".join(cards)}
<div class="legend"><b>Fair</b> = Pinnacle de-vigged probability, normalized within each market ·
 <b>Kalshi</b> = live order-book mid · <b>Edge</b> = Fair &minus; Kalshi in points (green = Kalshi
 underprices the fighter). Kalshi comparison covers the moneyline; click <b>Moneyline &#8599;</b> for
 the market. Round of <i>victory</i> omitted (not priced by Pinnacle); Round of <i>finish</i> = which
 round it ends, either fighter. Built from Pinnacle guest API league {LEAGUE}.</div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="force card date YYYY-MM-DD (UTC)")
    ap.add_argument("--json", action="store_true", help="also dump raw probabilities to stdout")
    args = ap.parse_args()

    print("Fetching Pinnacle UFC matchups + prices ...")
    mu, by_design, by_part = fetch()
    card_date, fights = build_card(mu, by_design, by_part, args.date)
    print("Fetching Kalshi UFC moneylines ...")
    attach_kalshi(fights, card_date)
    n_k = sum(1 for f in fights if f.get("kalshi"))
    print(f"Card {card_date}: {len(fights)} fights | {n_k} matched to Kalshi")
    for f in fights:
        got = ", ".join(k for k, _ in MARKET_TITLES if k in f["markets"])
        print(f"  {f['away']} vs {f['home']:30s} [{got}]")
    fetched = datetime.now().astimezone().strftime("%b %-d, %Y · %-I:%M %p %Z")
    open(OUT, "w", encoding="utf-8").write(render(card_date, fights, fetched))
    print(f"\nWrote {OUT}")
    if args.json:
        print(json.dumps(fights, indent=2))
