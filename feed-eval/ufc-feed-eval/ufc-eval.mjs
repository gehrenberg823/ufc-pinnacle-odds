// ============================================================================
//  UFC feed evaluation tool  —  Bolt (playbyplay) vs Kalshi (public odds)
//  Standalone. Everything hardcoded. Just run:  node ufc-eval.mjs
//  Then open the URL it prints (http://localhost:8899).
//
//  What it does:
//   • Polls Kalshi's PUBLIC api for the UFC fight-winner markets (no Kalshi key
//     needed) and tracks each fighter's implied win% as it moves.
//   • Connects to Bolt's play-by-play WebSocket for the same fights and streams
//     the live fight data next to the Kalshi odds so you can judge, in real
//     time, whether Bolt's UFC data is good (does the market move when Bolt
//     says something happened, and how fast).
//   • Saves everything raw to ./captures/*.jsonl for later inspection.
//
//  NOTE: the Bolt key below is a TRIAL key that rotates at end of Sunday, so
//  it is hardcoded here on purpose — nothing sensitive.
//  Requires Node 22+ (built-in WebSocket). On older Node: `npm i ws` first.
// ============================================================================

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// ---- hardcoded config -------------------------------------------------------
const BOLT_KEY   = 'd32f7092-93a6-4585-967a-54696352d030';   // trial, rotates Sun
const KALSHI     = 'https://api.elections.kalshi.com/trade-api/v2';
const BOLT_WS    = 'wss://spro.agency/api/playbyplay';
const BOLT_REST  = 'https://spro.agency/api';
const SERIES     = 'KXUFCFIGHT';   // Kalshi UFC fight-winner series
const ROUNDS_SER = 'KXUFCROUNDS';  // "fight ends before round N" props
const MOV_SER    = 'KXUFCMOV';     // method-of-victory props ("Fighter by KO/Sub/Decision")
const PORT       = 8899;
const PACE_MS    = 200;            // one Kalshi orderbook request per this interval (rate-safe pacing)

const WS = globalThis.WebSocket ?? await import('ws').then(m => m.WebSocket).catch(() => null);

const __dir  = path.dirname(fileURLToPath(import.meta.url));
const capDir = path.join(__dir, 'captures');
fs.mkdirSync(capDir, { recursive: true });
const stamp    = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const boltRaw  = fs.createWriteStream(path.join(capDir, `bolt-ufc-${stamp}.jsonl`),  { flags: 'a' });
const kalRaw   = fs.createWriteStream(path.join(capDir, `kalshi-ufc-${stamp}.jsonl`), { flags: 'a' });

// ---- state ------------------------------------------------------------------
const fights = new Map();   // eventTicker -> fight
let   eventDate = '';
let   kalshiOk = false, kalshiErr = '', boltOk = false;
let   clients  = [];        // SSE responses
let   dirty    = true;
let   currentWs = null;     // live Bolt socket (for re-subscribe)
const activity = [];        // trailing live log of updates (newest first)
function logEvent(feed, fight, text) {
  if (!text) return;
  activity.unshift({ t: Date.now(), feed, fight, text });
  if (activity.length > 80) activity.pop();
}

const MONTHS = { JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11 };
const dateTok   = ev => (ev.split('-')[1] || '').match(/^\d{2}[A-Z]{3}\d{2}/)?.[0] || '';
const parseTok  = t  => { const m = t.match(/^(\d{2})([A-Z]{3})(\d{2})/); return m ? new Date(2000 + +m[3], MONTHS[m[2]], +m[1]).getTime() : Infinity; };
const last3     = full => { const p = String(full).trim().split(/\s+/); return (p[p.length - 1] || '').slice(0, 3).toUpperCase(); };
const codesOf   = ev => { const suf = (ev.split('-')[1] || '').replace(/^\d{2}[A-Z]{3}\d{2}/, ''); return new Set([suf.slice(0, 3), suf.slice(3, 6)]); };
const fightByCodes = cs => { for (const f of fights.values()) if (cs.has(f.a.code) && cs.has(f.b.code)) return f; return null; };

async function jget(url) {
  const r = await fetch(url, { headers: { accept: 'application/json' } });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

function impliedPct(m) {                       // Kalshi prices are cents = %
  const { yes_bid: b, yes_ask: a, last_price: lp } = m;
  if (b != null && a != null && (b || a)) return (b + a) / 2;
  if (lp != null) return lp;
  return null;
}
function fighterFromMarket(m) {
  return { name: m.yes_sub_title || m.ticker, ticker: m.ticker, code: (m.ticker.split('-').pop() || '').toUpperCase(),
           pct: impliedPct(m), bid: m.yes_bid, ask: m.yes_ask, last: m.last_price, hist: [], lastMove: 0 };
}

// ---- discovery: which fights, and map Bolt game <-> Kalshi fight ------------
async function discover() {
  const d = await jget(`${KALSHI}/markets?series_ticker=${SERIES}&status=open&limit=400`);
  const byEv = new Map();
  for (const m of d.markets) (byEv.get(m.event_ticker) || byEv.set(m.event_ticker, []).get(m.event_ticker)).push(m);

  // soonest card date
  const dates = [...byEv.keys()].map(dateTok).filter(Boolean).sort((x, y) => parseTok(x) - parseTok(y));
  eventDate = dates[0] || '';

  for (const [ev, mk] of byEv) {
    if (dateTok(ev) !== eventDate || mk.length < 2) continue;
    fights.set(ev, { ev, a: fighterFromMarket(mk[0]), b: fighterFromMarket(mk[1]),
                     rounds: new Map(), mov: new Map(), boltGame: null, boltState: '', boltEvents: [], boltUpdate: 0 });
  }

  // Bolt games for that date, matched by the two 3-letter fighter codes
  let ufc = [];
  try { const g = await jget(`${BOLT_REST}/get_games?key=${BOLT_KEY}`); ufc = Object.values(g).filter(x => x && x.sport === 'UFC' && x.game); }
  catch (e) { console.log('WARN: Bolt get_games failed:', e.message); }

  mapBoltGames(ufc);
  return [...fights.values()];
}

// Collect ALL matching Bolt game entries per fight (Bolt has duplicate orderings /
// ids) so a wrong single pick can't make a fight look unsupported. Idempotent — safe
// to call repeatedly (periodic re-discovery adds any new ids).
function mapBoltGames(ufc) {
  for (const f of fights.values()) {
    if (!f.boltGames) { f.boltGames = []; f.boltUids = new Set(); }
    const want = new Set([f.a.code, f.b.code]);
    for (const g of ufc) {
      const codes = new Set(g.game.split(',')[0].split(' vs ').map(last3));   // strip ", DATE, id"
      if ([...want].every(c => codes.has(c)) && !f.boltUids.has(g.universal_id)) {
        f.boltUids.add(g.universal_id); f.boltGames.push(g.game);
      }
    }
    f.boltGame = f.boltGames[0] || null;   // for display
  }
}

// ---- orderbook pricing ------------------------------------------------------
// Kalshi's market summary bid/ask are null pre-trade; the live odds live in the
// orderbook (orderbook_fp: yes_dollars / no_dollars, prices in dollars). Best YES
// ask is derived from the best NO bid (a NO bid at p = a YES ask at 1-p).
async function obBidAsk(ticker) {
  const d = await jget(`${KALSHI}/markets/${encodeURIComponent(ticker)}/orderbook`);
  const fp = d.orderbook_fp || {};
  const yd = fp.yes_dollars || [], nd = fp.no_dollars || [];
  const bestY = yd.length ? Math.max(...yd.map(a => parseFloat(a[0]))) * 100 : null;
  const bestN = nd.length ? Math.max(...nd.map(a => parseFloat(a[0]))) * 100 : null;
  const bid = bestY != null ? Math.round(bestY) : null;
  const ask = bestN != null ? Math.round(100 - bestN) : null;
  return (bid == null && ask == null) ? null : { bid, ask };
}
async function runCapped(items, cap, fn) {
  const q = items.slice();
  await Promise.all(Array.from({ length: cap }, async () => { while (q.length) { try { await fn(q.shift()); } catch {} } }));
}
function applyPrice(fr, bid, ask, now, label) {
  const pct = (bid != null && ask != null) ? (bid + ask) / 2 : (bid ?? ask);
  if (pct != null && Math.round(pct) !== Math.round(fr.pct ?? -999)) {
    if (label && fr.pct != null) logEvent('kalshi', label, `${fr.name} ${Math.round(fr.pct)}% → ${Math.round(pct)}%`);
    fr.lastMove = now; fr.hist.push({ t: now, p: pct }); if (fr.hist.length > 240) fr.hist.shift();
  }
  fr.pct = pct; fr.bid = bid; fr.ask = ask;
}

function applyProp(r, bid, ask, now) {
  const pct = (bid != null && ask != null) ? (bid + ask) / 2 : (bid ?? ask);
  if (pct != null && Math.round(pct) !== Math.round(r.pct ?? -999)) { r.lastMove = now; if (r.hist) { r.hist.push(pct); if (r.hist.length > 80) r.hist.shift(); } }
  r.pct = pct; r.bid = bid; r.ask = ask;
}

// ---- discover prop markets (rounds + method-of-victory) and attach to fights -
async function fetchProps() {
  try {
    const d = await jget(`${KALSHI}/markets?series_ticker=${ROUNDS_SER}&limit=1000`);
    for (const m of d.markets.filter(m => dateTok(m.event_ticker) === eventDate)) {
      const f = fightByCodes(codesOf(m.event_ticker)); if (!f) continue;
      const rn = parseInt(m.ticker.split('-').pop(), 10); if (!rn) continue;
      if (!f.rounds.has(rn)) f.rounds.set(rn, { round: rn, ticker: m.ticker, pct: null, bid: null, ask: null, hist: [], lastMove: 0 });
    }
  } catch {}
  try {
    const d = await jget(`${KALSHI}/markets?series_ticker=${MOV_SER}&limit=1000`);
    for (const m of d.markets.filter(m => dateTok(m.event_ticker) === eventDate)) {
      const f = fightByCodes(codesOf(m.event_ticker)); if (!f || f.mov.has(m.ticker)) continue;
      const [who, method] = (m.yes_sub_title || '').split(/\s+by\s+/i);
      const c = who ? last3(who) : '';
      const side = c === f.a.code ? 'a' : c === f.b.code ? 'b' : null;
      f.mov.set(m.ticker, { ticker: m.ticker, side, method: (method || m.ticker.split('-').pop()).trim(), pct: null, bid: null, ask: null, lastMove: 0 });
    }
  } catch {}
  rebuildTargets();
}

// ---- paced, rate-safe price scheduler --------------------------------------
// Kalshi 429s on bursts, so we pace: ONE orderbook request every PACE_MS, cycling
// through all markets (winners weighted 3x for freshness), backing off on 429.
let targets = [], ti = 0, paceDelay = PACE_MS;
function rebuildTargets() {
  const wins = [...fights.values()].map(f => ({ ticker: f.a.ticker, apply: (bid, ask, now) => {
    applyPrice(f.a, bid, ask, now, `${f.a.name} v ${f.b.name}`);
    applyPrice(f.b, ask != null ? 100 - ask : null, bid != null ? 100 - bid : null, now);
  }}));
  const props = [];
  for (const f of fights.values()) {
    for (const r of f.rounds.values()) props.push({ ticker: r.ticker, apply: (bid, ask, now) => applyProp(r, bid, ask, now) });
    for (const mv of f.mov.values()) props.push({ ticker: mv.ticker, apply: (bid, ask, now) => applyProp(mv, bid, ask, now) });
  }
  targets = [...wins, ...wins, ...wins, ...props];
}
async function priceTick() {
  if (!targets.length) { setTimeout(priceTick, 500); return; }
  const item = targets[ti % targets.length]; ti++;
  try {
    const ob = await obBidAsk(item.ticker);
    if (ob) { item.apply(ob.bid, ob.ask, Date.now()); dirty = true; }
    kalshiOk = true; kalshiErr = ''; paceDelay = PACE_MS;
  } catch (e) {
    if (/429/.test(String(e.message))) { paceDelay = Math.min(2500, Math.round(paceDelay * 1.4)); kalshiErr = 'rate-limited (slowing)'; }
    else { kalshiErr = String(e.message); }
    kalshiOk = false;
  }
  setTimeout(priceTick, paceDelay);
}

// ---- Bolt WebSocket ---------------------------------------------------------
const uidFromStr = s => (String(s).match(/([0-9a-f]{10,})/i) || [])[1] || null;
function fightByUid(uid) { if (!uid) return null; for (const f of fights.values()) if (f.boltUids && f.boltUids.has(uid)) return f; return null; }
function summarizeBolt(m) {
  const parts = [];
  const pi = m.play_info, sc = m.score;
  if (pi && typeof pi === 'object' && !Array.isArray(pi)) {                 // MMA match_state / object play_info
    if (pi.status && pi.status !== 'NotStarted') parts.push(pi.status);
    if (pi.round != null) parts.push('R' + pi.round);
    if (pi.clock) parts.push(pi.clock);
    if (pi.result) parts.push(typeof pi.result === 'object' ? (pi.result.method || pi.result.winner || JSON.stringify(pi.result).slice(0, 50)) : String(pi.result));
    if (!parts.length && pi.status) parts.push(pi.status);
    if (!parts.length) parts.push(JSON.stringify(pi).slice(0, 80));
  } else if (Array.isArray(pi)) {                                          // stream 1/2/3 event arrays
    for (const p of pi.slice(-2)) if (p && typeof p === 'object') parts.push(p.description || p.title || p.type || JSON.stringify(p).slice(0, 70));
  }
  if (sc && (sc.home != null || sc.away != null)) parts.push(`score ${sc.away ?? '?'}-${sc.home ?? '?'}`);
  return parts.join(' · ') || m.action || '';
}
function boltGamesList() { return [...new Set([...fights.values()].flatMap(f => f.boltGames || []))]; }
function connectBolt() {
  if (!WS) { console.log('\n!! No WebSocket in this Node. Run `npm i ws` (or use Node 22+), then re-run.\n'); return; }
  if (!boltGamesList().length) { console.log('WARN: no Bolt games mapped — Bolt panel will stay empty.'); }
  const open = () => {
    let ws;
    try { ws = new WS(`${BOLT_WS}?key=${BOLT_KEY}`); } catch (e) { setTimeout(open, 5000); return; }
    currentWs = ws;
    ws.onopen = () => { boltOk = true; try { ws.send(JSON.stringify({ action: 'subscribe', filters: { games: boltGamesList() } })); } catch {} dirty = true; };
    ws.onmessage = async (e) => {   // Node's WebSocket delivers frames as Blob — must decode to text
      let raw;
      try { raw = (typeof e.data === 'string') ? e.data : (e.data && e.data.text ? await e.data.text() : Buffer.isBuffer(e.data) ? e.data.toString('utf8') : String(e.data)); }
      catch { return; }
      handleBolt(raw);
    };
    ws.onclose = () => { boltOk = false; dirty = true; setTimeout(open, 5000); };
    ws.onerror = () => {};
  };
  open();
}
function handleBolt(raw) {
  let m; try { m = JSON.parse(raw); } catch { boltRaw.write(JSON.stringify({ t: Date.now(), raw }) + '\n'); return; }
  boltRaw.write(JSON.stringify({ t: Date.now(), msg: m }) + '\n');
  if (!m || typeof m !== 'object') { dirty = true; return; }
  // per-fight "not supported" ({error:...}) or "Cannot subscribe... Code 1" ({action:error, message})
  if ((m.error && !m.action) || m.action === 'error') {
    const txt = m.error || m.message || '';
    const f = fightByUid(uidFromStr(txt));
    // only mark unsupported if NO id for this fight is delivering (a duplicate id may still work)
    if (f && !f.boltUpdate) { f.boltUnsupported = true; f.boltState = /not supported/i.test(txt) ? 'PBP not supported' : 'subscribe rejected'; }
    dirty = true; return;
  }
  if (m.action !== 'new_play' && m.action !== 'stats' && m.action !== 'match_state') { dirty = true; return; }
  const f = fightByUid(m.universal_id) || (m.event ? [...fights.values()].find(x => x.boltGames && x.boltGames.includes(m.event)) : null);
  if (f) {
    const now = Date.now(), text = summarizeBolt(m);
    const changed = text && text !== f.boltState;
    f.boltUpdate = now; f.boltUnsupported = false; if (text) f.boltState = text;
    if ((m.action === 'new_play' || m.action === 'match_state') && text) { f.boltEvents.unshift({ t: now, d: text }); if (f.boltEvents.length > 14) f.boltEvents.pop(); }
    if (changed) logEvent('bolt', `${f.a.name} v ${f.b.name}`, text);
  }
  dirty = true;
}

// ---- serialize + SSE broadcast ---------------------------------------------
function serialize() {
  const now = Date.now();
  const list = [...fights.values()].map(f => ({
    ev: f.ev, boltGame: f.boltGame, boltState: f.boltState, boltUpdate: f.boltUpdate, boltUnsupported: !!f.boltUnsupported,
    boltAge: f.boltUpdate ? Math.round((now - f.boltUpdate) / 1000) : null,
    boltEvents: f.boltEvents,
    a: sideJson(f.a, now), b: sideJson(f.b, now),
    rounds: [...f.rounds.values()].sort((x, y) => x.round - y.round)
              .map(r => ({ round: r.round, pct: r.pct, bid: r.bid, ask: r.ask, moveAge: r.lastMove ? Math.round((now - r.lastMove) / 1000) : null })),
    mov: [...f.mov.values()].filter(m => m.side && m.pct != null)
              .map(m => ({ side: m.side, method: m.method, pct: m.pct, bid: m.bid, ask: m.ask }))
              .sort((x, y) => (y.pct || 0) - (x.pct || 0)),
  }));
  // live fights (bolt updating) first, then by soonest move
  list.sort((x, y) => (y.boltUpdate || 0) - (x.boltUpdate || 0));
  const boltSupported = list.filter(f => !f.boltUnsupported && f.boltState).length;
  return JSON.stringify({ now, eventDate, kalshiOk, kalshiErr, boltOk, wsAvail: !!WS, boltSupported, fights: list,
    activity: activity.slice(0, 60) });
}
function sideJson(fr, now) {
  return { name: fr.name, code: fr.code, pct: fr.pct, bid: fr.bid, ask: fr.ask, last: fr.last, status: fr.status,
           moveAge: fr.lastMove ? Math.round((now - fr.lastMove) / 1000) : null,
           hist: fr.hist.slice(-80).map(h => Math.round(h.p)) };
}
setInterval(() => { if (!dirty) return; dirty = false; const p = serialize(); for (const c of clients) { try { c.write(`data: ${p}\n\n`); } catch {} } }, 250);

// ---- http server + dashboard ------------------------------------------------
const server = http.createServer((req, res) => {
  if (req.url === '/stream') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
    res.write(`data: ${serialize()}\n\n`);
    clients.push(res);
    req.on('close', () => { clients = clients.filter(c => c !== res); });
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(PAGE);
});

// ---- boot -------------------------------------------------------------------
console.log('UFC feed eval — discovering fights…');
const fs2 = await discover();
console.log(`\nEvent date token: ${eventDate}   (${fs2.length} fights)`);
for (const f of fs2) console.log(`  ${f.a.name}  vs  ${f.b.name}   ${(f.boltGames && f.boltGames.length) ? '✓ Bolt mapped x' + f.boltGames.length : '✗ no Bolt game'}`);
await fetchProps();              // discover round + method-of-victory markets
rebuildTargets(); priceTick();  // start the paced, rate-safe price scheduler
setInterval(fetchProps, 60000); // periodically pick up newly-opened prop markets
connectBolt();
// periodic re-discovery: re-map Bolt games and re-subscribe on the live socket (no new
// connection), so fights/ids that come online later get picked up automatically.
async function refreshBolt() {
  try {
    const gm = await jget(`${BOLT_REST}/get_games?key=${BOLT_KEY}`);
    mapBoltGames(Object.values(gm).filter(x => x && x.sport === 'UFC' && x.game));
    if (currentWs && currentWs.readyState === 1) currentWs.send(JSON.stringify({ action: 'subscribe', filters: { games: boltGamesList() } }));
  } catch {}
}
setInterval(refreshBolt, 90000);
server.listen(PORT, () => console.log(`\n➡  Open  http://localhost:${PORT}\n   Raw capture → ${capDir}\n   (Ctrl+C to stop)`));

// ---- dashboard page (inline, self-contained) --------------------------------
const PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>UFC feed eval — Bolt vs Kalshi</title>
<style>
:root{--bg:#0b1020;--panel:#131b30;--panel2:#0e1526;--line:#243350;--ink:#e9eef8;--mut:#8ea0bb;--faint:#5b6b85;
 --bolt:#f5a524;--kal:#35c0c9;--up:#3ecf8e;--dn:#f5677a;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans)}
header{position:sticky;top:0;background:linear-gradient(180deg,#0b1020,#0b1020ee);border-bottom:1px solid var(--line);padding:12px 18px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;z-index:5}
h1{font-size:16px;margin:0;font-weight:800;letter-spacing:-.01em}
h1 .k{color:var(--kal)}h1 .b{color:var(--bolt)}
.pill{font:600 11px/1 var(--mono);padding:5px 9px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
.pill.on{color:#04121a;background:var(--up);border-color:transparent}.pill.off{color:#fff;background:var(--dn);border-color:transparent}
.clock{margin-left:auto;font:600 13px/1 var(--mono);color:var(--mut)}
.wrap{max-width:1180px;margin:0 auto;padding:18px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;overflow:hidden}
.card.live{border-color:var(--bolt);box-shadow:0 0 0 1px var(--bolt)}
.ctop{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-bottom:1px solid var(--line)}
.ctop .ev{font:600 10px/1 var(--mono);letter-spacing:.08em;color:var(--faint)}
.badge{font:700 9px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase;padding:4px 7px;border-radius:5px}
.badge.live{color:#04121a;background:var(--bolt)}.badge.pre{color:var(--mut);border:1px solid var(--line)}.badge.na{color:#fff;background:var(--dn)}
.fighters{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.fighter{background:var(--panel);padding:13px 14px}
.fighter .nm{font-weight:700;font-size:14px;margin-bottom:6px}
.fighter .pct{font-family:var(--mono);font-size:26px;font-weight:800;letter-spacing:-.02em;color:var(--kal);font-variant-numeric:tabular-nums}
.fighter .pct.none{color:var(--faint);font-size:16px;font-weight:600}
.fighter .ynrow{display:flex;gap:7px;margin-top:9px}
.fighter .yn{flex:1;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:8px;font-family:var(--mono);border:1px solid var(--line)}
.fighter .yn b{font-size:10px;font-weight:700;letter-spacing:.09em}
.fighter .yn .c{font-size:17px;font-weight:800;font-variant-numeric:tabular-nums}
.fighter .yn.yes{color:var(--up);border-color:color-mix(in srgb,var(--up) 42%,transparent);background:color-mix(in srgb,var(--up) 10%,transparent)}
.fighter .yn.no{color:var(--dn);border-color:color-mix(in srgb,var(--dn) 42%,transparent);background:color-mix(in srgb,var(--dn) 10%,transparent)}
.fighter .mv{font:10px/1 var(--mono);color:var(--faint);margin-top:7px}
.spark{height:34px;margin-top:8px}
.rounds{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 14px;border-top:1px solid var(--line);background:var(--panel)}
.rounds .rl{font:600 9.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.rp{font:12px/1 var(--mono);color:var(--mut);border:1px solid var(--line);border-radius:6px;padding:5px 8px;display:inline-flex;align-items:center;gap:5px}
.rp b{color:var(--ink);font-weight:700}
.rp .ynm{display:inline-flex;gap:3px}
.rp .ynm i{font-style:normal;font-size:9.5px;font-weight:700;padding:1px 4px;border-radius:4px;font-variant-numeric:tabular-nums}
.rp .ynm .y{color:var(--up);background:color-mix(in srgb,var(--up) 15%,transparent)}
.rp .ynm .n{color:var(--dn);background:color-mix(in srgb,var(--dn) 15%,transparent)}
.mvdot{width:6px;height:6px;border-radius:50%;background:var(--up);display:inline-block}
.bolt{padding:11px 14px;border-top:1px solid var(--line);background:var(--panel2)}
.bolt .h{display:flex;justify-content:space-between;align-items:center;font:600 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px}
.bolt .h .tag{color:var(--bolt)}
.bolt .log{display:flex;flex-direction:column;gap:5px;max-height:150px;overflow:auto}
.ev-row{font-size:12px;display:flex;gap:8px}
.ev-row .t{font-family:var(--mono);color:var(--faint);font-size:10.5px;white-space:nowrap}
.bolt .empty{font-size:12px;color:var(--faint);font-style:italic}
.flash{animation:fl .8s ease}@keyframes fl{from{background:#f5a52433}to{background:transparent}}
.note{color:var(--faint);font-size:12px;margin:14px 2px}
.logpanel{background:var(--panel2);border:1px solid var(--line);border-radius:12px;margin-bottom:14px;overflow:hidden}
.logh{display:flex;justify-content:space-between;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);font:600 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.faint{color:var(--faint)}
.logbody{max-height:190px;overflow:auto;padding:6px 0}
.logrow{display:flex;gap:10px;align-items:baseline;padding:3px 14px;font-size:12.5px}
.logfilter{display:flex;align-items:center;gap:6px}
.fbtn{font:600 9.5px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--mut);background:transparent;border:1px solid var(--line);border-radius:5px;padding:4px 8px;cursor:pointer}
.fbtn:hover{color:var(--ink)}
.fbtn.on{color:#04121a;background:var(--mut);border-color:transparent}
.fbtn.on[data-f=kalshi]{background:var(--kal)}.fbtn.on[data-f=bolt]{background:var(--bolt)}
.logrow .lt{font-family:var(--mono);font-size:10.5px;color:var(--faint);white-space:nowrap}
.logrow .lf{font:600 9px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:2px 5px;border-radius:4px;white-space:nowrap}
.logrow .lf.bolt{color:var(--bolt);background:color-mix(in srgb,var(--bolt) 15%,transparent)}
.logrow .lf.kalshi{color:var(--kal);background:color-mix(in srgb,var(--kal) 15%,transparent)}
.logrow .lg{color:var(--faint);white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.logrow .ld{color:var(--ink)}
.logempty{padding:14px;color:var(--faint);font-style:italic;font-size:12.5px}
</style></head><body>
<header>
  <h1><span class="k">Kalshi</span> odds × <span class="b">Bolt</span> UFC data</h1>
  <span class="pill" id="p-event">—</span>
  <span class="pill off" id="p-kal">Kalshi</span>
  <span class="pill off" id="p-bolt">Bolt</span>
  <span class="clock" id="clock">—</span>
</header>
<div class="wrap">
  <div class="logpanel"><div class="logh"><span>Live activity — every update as it streams in</span><span class="logfilter"><button class="fbtn on" data-f="all">all</button><button class="fbtn" data-f="kalshi">kalshi</button><button class="fbtn" data-f="bolt">bolt</button><span id="logcount" class="faint"></span></span></div><div class="logbody" id="activity"><div class="logempty">waiting for updates…</div></div></div>
  <div class="grid" id="grid"></div>
  <p class="note" id="note">Waiting for data… Kalshi odds populate when markets go live; Bolt fight data streams during the fights. Everything is also saved raw to ./captures/.</p>
</div>
<script>
const $=s=>document.querySelector(s);
function spark(hist){
  if(!hist||hist.length<2) return '';
  const w=180,h=34,lo=Math.min(...hist),hi=Math.max(...hist),rng=Math.max(1,hi-lo);
  const pts=hist.map((p,i)=>[ (i/(hist.length-1))*w, h-2-((p-lo)/rng)*(h-4) ]);
  const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const up=hist[hist.length-1]>=hist[0];
  return '<svg class="spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" style="width:100%"><path d="'+d+'" fill="none" stroke="'+(up?'var(--up)':'var(--dn)')+'" stroke-width="1.5"/></svg>';
}
function pctCell(fr){
  if(fr.pct==null) return '<div class="fighter"><div class="nm">'+fr.name+'</div><span class="pct none">— no market</span><div class="mv">'+(fr.status||'')+'</div></div>';
  const yesBuy = fr.ask, noBuy = (fr.bid!=null ? 100-fr.bid : null);   // cost to take each side
  const yn = '<div class="ynrow">'
    + '<span class="yn yes"><b>YES</b><span class="c">'+(yesBuy!=null?yesBuy+'¢':'–')+'</span></span>'
    + '<span class="yn no"><b>NO</b><span class="c">'+(noBuy!=null?noBuy+'¢':'–')+'</span></span></div>';
  const mv = fr.moveAge==null ? '' : 'last move '+fr.moveAge+'s ago';
  return '<div class="fighter"><div class="nm">'+fr.name+'</div><span class="pct">'+Math.round(fr.pct)+'%</span>'+yn+'<div class="mv">'+mv+'</div>'+spark(fr.hist)+'</div>';
}
function ynMini(o){  // YES=ask (buy), NO=100-bid — same take framing as the fighter quotes
  if(o.ask==null&&o.bid==null) return '';
  const y=o.ask!=null?o.ask+'¢':'–', n=o.bid!=null?(100-o.bid)+'¢':'–';
  return '<span class="ynm"><i class="y">Y '+y+'</i><i class="n">N '+n+'</i></span>';
}
function card(f){
  const live = f.boltAge!=null && f.boltAge<120;
  const badge = f.boltUnsupported ? '<span class="badge na">Bolt PBP n/a</span>' : live ? '<span class="badge live">Bolt live '+f.boltAge+'s</span>' : '<span class="badge pre">'+(f.boltGame?'awaiting':'no bolt')+'</span>';
  const log = (f.boltEvents&&f.boltEvents.length)
    ? f.boltEvents.map(e=>'<div class="ev-row"><span class="t">'+new Date(e.t).toLocaleTimeString()+'</span><span>'+e.d+'</span></div>').join('')
    : '<div class="empty">'+(f.boltGame?('subscribed — '+(f.boltState||'no plays yet')):'no Bolt game mapped')+'</div>';
  return '<div class="card'+(live?' live':'')+'" data-ev="'+f.ev+'"><div class="ctop"><span class="ev">'+f.ev+'</span>'+badge+'</div>'+
    '<div class="fighters">'+pctCell(f.a)+pctCell(f.b)+'</div>'+
    ((f.rounds&&f.rounds.length)?('<div class="rounds"><span class="rl">finish before</span>'+f.rounds.map(r=>'<span class="rp">R'+r.round+' <b>'+(r.pct==null?'-':Math.round(r.pct)+'%')+'</b>'+ynMini(r)+(r.moveAge!=null&&r.moveAge<20?'<i class="mvdot"></i>':'')+'</span>').join('')+'</div>'):'')+
    ((f.mov&&f.mov.length)?('<div class="rounds"><span class="rl">by method</span>'+f.mov.slice(0,8).map(m=>{var nm=(m.side==="a"?f.a.name:f.b.name).split(" ").pop().slice(0,4);var mt=m.method.replace(/KO.?TKO/i,"KO").replace(/Submission/i,"Sub").replace(/Decision|Points/i,"Dec").slice(0,4);return "<span class=\\"rp\\" style=\\"border-left:2px solid "+(m.side==="a"?"var(--kal)":"var(--bolt)")+"\\">"+nm+" "+mt+" <b>"+Math.round(m.pct)+"%</b>"+ynMini(m)+"</span>";}).join("")+"</div>"):"")+
    '<div class="bolt"><div class="h"><span><span class="tag">Bolt</span> play-by-play</span><span>'+(f.boltAge!=null?('upd '+f.boltAge+'s'):'')+'</span></div><div class="log">'+log+'</div></div></div>';
}
let prev={};
const es=new EventSource('/stream');
es.onmessage=(e)=>{
  const d=JSON.parse(e.data);
  $('#p-event').textContent = d.eventDate ? ('UFC · '+d.eventDate) : 'discovering…';
  $('#p-kal').className='pill '+(d.kalshiOk?'on':'off'); $('#p-kal').textContent='Kalshi '+(d.kalshiOk?'polling':'down');
  $('#p-bolt').className='pill '+(d.boltOk?'on':'off'); $('#p-bolt').textContent='Bolt '+(d.boltOk?('connected · '+(d.boltSupported||0)+'/'+d.fights.length+' fights'):(d.wsAvail?'connecting':'no WS'));
  $('#clock').textContent=new Date(d.now).toLocaleTimeString();
  $('#note').style.display = d.fights.length ? 'none':'block';
  const g=$('#grid');
  g.innerHTML=d.fights.map(card).join('');
  for(const f of d.fights){ if(prev[f.ev]!==undefined && f.boltUpdate>prev[f.ev]){ const el=g.querySelector('[data-ev="'+f.ev+'"]'); if(el) el.classList.add('flash'); } prev[f.ev]=f.boltUpdate; }
  // live activity log
  lastAct=d.activity||[]; renderLog();
};
let lastAct=[], logFilter='all';
function renderLog(){
  const act=lastAct.filter(e=>logFilter==='all'||e.feed===logFilter);
  $('#logcount').textContent = act.length ? (act.length+' recent') : '';
  const ab=$('#activity');
  ab.innerHTML = act.length ? act.map(e=>'<div class="logrow"><span class="lt">'+new Date(e.t).toLocaleTimeString()+'</span><span class="lf '+e.feed+'">'+e.feed+'</span><span class="lg">'+(e.fight||'')+'</span><span class="ld">'+e.text+'</span></div>').join('') : '<div class="logempty">'+(lastAct.length?'no '+logFilter+' updates yet…':'waiting for updates… (odds moves + Bolt fight events appear here as they stream)')+'</div>';
}
for(const b of document.querySelectorAll('.fbtn')) b.onclick=()=>{ logFilter=b.dataset.f; for(const x of document.querySelectorAll('.fbtn')) x.classList.toggle('on',x===b); renderLog(); };
es.onerror=()=>{ $('#p-kal').textContent='reconnecting…'; };
</script></body></html>`;
