"""
FPL API - a small web service that returns live Fantasy Premier League data.

Needs fpl_fetch.py in the same folder (it reuses that file's code).

Open the main address in a browser to see the dashboard (web app).

Endpoints (all return JSON unless noted):
    /api                   list of endpoints
    /status                current + next gameweek and deadlines
    /team/<id>             a manager's summary and squad  (optional ?gw=10)
    /transfers/<id>        rule-based transfer ideas  (?ft=1&horizon=4&bank=3.6)
    /players               players list  (?sort=form&limit=20&position=MID&team=ARS)
    /fixtures              fixture difficulty per team  (?from_gw=12&n=5)
    /snapshot              full readable report as text/markdown (default team ID)
    /snapshot/<id>         same, for another team ID    (optional ?gw=10)

Optional password: set an environment variable API_KEY and every request must
add ?key=YOUR_KEY to the URL (or send an X-API-Key header).
"""

import os
import time

from flask import Flask, Response, jsonify, request

import fpl_fetch as fpl

app = Flask(__name__)

# ---- Caching: remember each FPL answer for 60 seconds so we don't spam FPL ----
CACHE_SECONDS = 60
_cache = {}
_original_get = fpl.get


def cached_get(path, retries=3):
    now = time.time()
    hit = _cache.get(path)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    data = _original_get(path, retries)
    _cache[path] = (now, data)
    return data


fpl.get = cached_get  # fpl_fetch's functions now use the cached version too


# ---- Optional password check ----
@app.before_request
def check_key():
    needed = os.environ.get("API_KEY")
    if not needed:
        return None
    given = request.args.get("key") or request.headers.get("X-API-Key")
    if given != needed:
        return jsonify(error="Missing or wrong key. Add ?key=YOUR_KEY to the URL."), 401
    return None


@app.errorhandler(RuntimeError)
def fpl_problem(e):
    return jsonify(error=str(e)), 502


# ---- Helpers ----
def load_boot():
    boot = fpl.get("bootstrap-static/")
    if not boot:
        raise RuntimeError("Could not load the main FPL data.")
    players = {p["id"]: p for p in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    events = boot["events"]
    current = next((e for e in events if e["is_current"]), None)
    upcoming = next((e for e in events if e["is_next"]), None)
    return players, teams, events, current, upcoming


def player_dict(p, teams, fixtures=None, from_gw=None):
    d = {
        "id": p["id"],
        "name": p["web_name"],
        "full_name": f"{p['first_name']} {p['second_name']}",
        "position": fpl.POSITIONS[p["element_type"]],
        "team": teams[p["team"]]["short_name"],
        "price": p["now_cost"] / 10,
        "form": float(p["form"]),
        "total_points": p["total_points"],
        "points_per_game": float(p["points_per_game"]),
        "expected_points_next": float(p["ep_next"]),
        "minutes": p["minutes"],
        "goals": p["goals_scored"],
        "assists": p["assists"],
        "clean_sheets": p["clean_sheets"],
        "xGI": float(p["expected_goal_involvements"]),
        "owned_by_percent": float(p["selected_by_percent"]),
        "transfers_in_this_gw": p["transfers_in_event"],
        "transfers_out_this_gw": p["transfers_out_event"],
        "price_change_this_gw": p["cost_change_event"] / 10,
        "status": p["status"],  # a=available d=doubtful i=injured s=suspended u=unavailable
        "news": p.get("news") or "",
        "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
    }
    if fixtures is not None:
        d["next_fixtures"] = fpl.fixtures_text(fixtures, teams, p["team"], from_gw)
    return d


# ---- Transfer suggestions (rule-based estimate, not an official FPL tool) ----
DIFF_MULT = {1: 1.25, 2: 1.12, 3: 1.0, 4: 0.88, 5: 0.75}  # fixture difficulty -> points multiplier
MIN_NET_GAIN = 2.0  # smallest net gain (points) worth acting on
HIT_COST = 4


def fixture_diffs(fixtures, team_id, from_gw, n):
    """List of (gameweek, difficulty) for a team in the look-ahead window."""
    out = []
    for f in fixtures:
        gw = f.get("event")
        if gw is None or gw < from_gw or gw >= from_gw + n:
            continue
        if f["team_h"] == team_id:
            out.append((gw, f["team_h_difficulty"]))
        elif f["team_a"] == team_id:
            out.append((gw, f["team_a_difficulty"]))
    return out


def first_gw_availability(p):
    c = p.get("chance_of_playing_next_round")
    if c is not None:
        return c / 100.0
    return {"a": 1.0, "d": 0.5}.get(p["status"], 0.0)


def later_gw_availability(p):
    return {"a": 1.0, "d": 0.9, "i": 0.5, "s": 0.5}.get(p["status"], 0.0)


def project(p, diffs, first_gw, finished_gws):
    """Rough projected points over the window: per-game rate x fixture difficulty x availability."""
    share = min(1.0, p["minutes"] / (90.0 * finished_gws)) if finished_gws else 1.0
    rate = 0.4 * float(p["ep_next"]) + 0.3 * float(p["form"]) + 0.3 * float(p["points_per_game"]) * share
    first_av, later_av = first_gw_availability(p), later_gw_availability(p)
    total = 0.0
    for gw, d in diffs:
        total += rate * DIFF_MULT.get(d, 1.0) * (first_av if gw == first_gw else later_av)
    return round(total, 1)


def selling_prices(squad_ids, players, transfers):
    """FPL's rule: you keep half of any price rise (rounded down). Prices are in tenths of a million."""
    last_in = {}
    for t in transfers or []:
        key = (t.get("event", 0), t.get("time", ""))
        e = t["element_in"]
        if e not in last_in or key >= last_in[e][0]:
            last_in[e] = (key, t["element_in_cost"])
    out = {}
    for eid in squad_ids:
        now = players[eid]["now_cost"]
        buy = last_in[eid][1] if eid in last_in else now - players[eid].get("cost_change_start", 0)
        out[eid] = now if now <= buy else buy + (now - buy) // 2
    return out


def brief(p, teams, fixtures, from_gw, n, projected, sell=None):
    d = {
        "id": p["id"],
        "name": p["web_name"],
        "team": teams[p["team"]]["short_name"],
        "position": fpl.POSITIONS[p["element_type"]],
        "price": p["now_cost"] / 10,
        "projected_points": projected,
        "form": float(p["form"]),
        "expected_points_next": float(p["ep_next"]),
        "next_fixtures": fpl.fixtures_text(fixtures, teams, p["team"], from_gw, n),
        "status": p["status"],
        "news": p.get("news") or "",
        "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
    }
    if sell is not None:
        d["sell_price"] = sell / 10
    return d


# ---- Endpoints ----
APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL Dashboard</title>
<style>
:root{--bg:#f6f4f8;--card:#fff;--ink:#1d1d1f;--mute:#6b6b76;--line:#e3e0ea;--brand:#37003c;--accent:#00a35a;--warn:#c2410c;--warnbg:#fff1e6}
@media (prefers-color-scheme:dark){:root{--bg:#14101a;--card:#1f1a28;--ink:#f1eef6;--mute:#a49fb2;--line:#322b3f;--brand:#4a1156;--accent:#2ee59d;--warn:#fb923c;--warnbg:#3a2314}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--brand);color:#fff;padding:14px 16px;display:flex;flex-wrap:wrap;gap:6px 24px;align-items:center;justify-content:space-between}
header h1{margin:0;font-size:20px}
#deadline{font-size:14px;text-align:right}
#deadline b{color:#00ff87}
nav{display:flex;gap:4px;padding:10px 12px 0;overflow-x:auto}
nav button{border:1px solid var(--line);border-bottom:none;background:var(--card);color:var(--mute);padding:9px 16px;border-radius:10px 10px 0 0;font:inherit;cursor:pointer;white-space:nowrap}
nav button.on{color:var(--ink);font-weight:600;border-top:3px solid var(--accent)}
main{background:var(--card);border-top:1px solid var(--line);padding:16px;min-height:60vh}
section{display:none;max-width:1000px;margin:0 auto}
section.on{display:block}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
input,select,button.act{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
input{width:110px}
button.act{background:var(--accent);color:#fff;border:none;font-weight:600;cursor:pointer}
.msg{color:var(--mute);padding:12px 0}
.err{color:var(--warn);background:var(--warnbg);padding:10px 12px;border-radius:8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat span{display:block;font-size:12px;color:var(--mute)}
.stat b{font-size:16px}
.stat small{display:block;color:var(--mute);font-size:12px}
.flags{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.flags ul{margin:4px 0 0;padding-left:18px}
.pitch{background:linear-gradient(#e9f7ee,#d4f0df);border-radius:12px;padding:10px 6px}
@media (prefers-color-scheme:dark){.pitch{background:linear-gradient(#17301f,#122618)}}
.row{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin:8px 0}
.card{flex:1 1 130px;max-width:165px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;text-align:center}
.card.flag{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn)}
.nm{font-weight:700}
.sub{font-size:12px;color:var(--mute)}
.ep{font-size:12px;display:flex;justify-content:center;gap:10px;margin:4px 0}
.bdg{display:inline-block;margin-left:4px;font-size:11px;border-radius:50%;width:18px;height:18px;line-height:18px;color:#fff}
.bdg.c{background:#111}.bdg.v{background:#6b6b76}
.benchbox{margin-top:14px;border:1px dashed var(--line);border-radius:12px;padding:6px}
.benchbox h3{margin:4px 8px;font-size:13px;color:var(--mute);font-weight:600}
.fxs{display:flex;gap:3px;justify-content:center;flex-wrap:wrap}
.fx{font-size:11px;font-weight:600;padding:2px 5px;border-radius:5px;display:inline-block}
.d1{background:#1b7f4b;color:#fff}.d2{background:#4cc27f;color:#06260f}.d3{background:#d9d9de;color:#222}.d4{background:#f08a4b;color:#2a1000}.d5{background:#d13b3b;color:#fff}
.rec{border:2px solid var(--accent);border-radius:12px;padding:12px 14px;margin-bottom:14px}
.rec.roll{border-color:var(--line)}
.rec h3{margin:0 0 4px;font-size:17px}
.move{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:10px 0;padding:8px;border:1px solid var(--line);border-radius:10px}
.mv{flex:1 1 200px}
.mv small{display:block;font-size:11px;font-weight:700;letter-spacing:.05em}
.mv.out small{color:var(--warn)}.mv.in small{color:var(--accent)}
.arrow{font-size:20px;color:var(--mute)}
.gain{font-weight:700;font-size:18px;color:var(--accent);min-width:60px;text-align:right}
.sell{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
.sell h4{margin:0 0 2px;font-size:15px}
.why{font-size:12px;color:var(--warn);margin-bottom:6px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mute);font-size:12px;font-weight:600}
td.blank{color:var(--mute);text-align:center}
.legend{font-size:12px;color:var(--mute);margin:8px 0}
textarea{width:100%;height:260px;font:12px/1.4 ui-monospace,Menlo,monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px}
</style>
</head>
<body>
<header>
  <h1>⚽ FPL Dashboard</h1>
  <div id="deadline">Loading deadline…</div>
</header>
<nav id="tabs">
  <button class="on" data-tab="team">My Team</button>
  <button data-tab="transfers">Transfers</button>
  <button data-tab="players">Players</button>
  <button data-tab="fixtures">Fixtures</button>
  <button data-tab="claude">For Claude</button>
</nav>
<main>
  <section id="team" class="on">
    <div class="bar">
      <label for="tid">Team ID</label>
      <input id="tid" inputmode="numeric" value="__DEFAULT_TEAM__">
      <button class="act" id="loadTeam">Load team</button>
    </div>
    <div id="teamOut"></div>
  </section>

  <section id="transfers">
    <div class="bar">
      <label for="tFt">Free transfers</label>
      <select id="tFt"><option>0</option><option selected>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select>
      <label for="tH">Look ahead</label>
      <select id="tH"><option value="3">3 GWs</option><option value="4" selected>4 GWs</option><option value="5">5 GWs</option><option value="6">6 GWs</option></select>
      <input id="tBank" placeholder="Bank £m (auto)" style="width:130px">
      <button class="act" id="loadTransfers">Get suggestions</button>
    </div>
    <p class="legend">Uses the team ID from the My Team tab. FPL doesn't share your free transfers, so set them above.</p>
    <div id="transfersOut"></div>
  </section>

  <section id="players">
    <div class="bar">
      <select id="pPos"><option value="">All positions</option><option>GK</option><option>DEF</option><option>MID</option><option>FWD</option></select>
      <select id="pSort">
        <option value="ep_next">Expected points next GW</option>
        <option value="form">Form</option>
        <option value="total_points">Total points</option>
        <option value="points_per_game">Points per game</option>
        <option value="xgi">xGI</option>
        <option value="price">Price</option>
        <option value="owned">Ownership</option>
        <option value="transfers_in">Transfers in</option>
      </select>
      <input id="pTeam" placeholder="Club e.g. ARS" maxlength="3" style="width:130px">
      <select id="pLimit"><option>20</option><option>40</option><option>100</option></select>
      <button class="act" id="loadPlayers">Show</button>
    </div>
    <div id="playersOut"></div>
  </section>

  <section id="fixtures">
    <div class="legend">Colour = FPL difficulty (1 easy, 5 hard). CAPITALS = home game, lowercase = away.</div>
    <div id="fixturesOut"></div>
  </section>

  <section id="claude">
    <p>Copy the full report and paste it into any Claude chat to get advice, for example if your project can't open the link.</p>
    <div class="bar"><button class="act" id="copySnap">Copy report for Claude</button><span id="copyMsg" class="msg"></span></div>
    <textarea id="snapTxt" readonly placeholder="The report will appear here"></textarea>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
const KEY = new URLSearchParams(location.search).get('key');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => '£' + Number(n).toFixed(1) + 'm';
const fmt = n => Number(n).toLocaleString('en-IN');
const WAIT = '<p class="msg">Loading… the free server can take up to a minute to wake up.</p>';

async function api(path) {
  let u = path;
  if (KEY) u += (u.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(KEY);
  const r = await fetch(u);
  if (!r.ok) {
    let m = '';
    try { m = (await r.json()).error; } catch (e) {}
    throw new Error(m || ('Error ' + r.status));
  }
  return r;
}

function parseFx(s) {
  const out = [];
  if (!s || s === '-') return out;
  const re = /GW(\d+) ([^\s(]+)\((H|A)\)\[(\d)\]/g;
  let m;
  while ((m = re.exec(s))) out.push({gw: +m[1], opp: m[2], ha: m[3], d: +m[4]});
  return out;
}
const fxSpan = f => `<span class="fx d${f.d}" title="GW${f.gw}">${esc(f.ha === 'H' ? f.opp.toUpperCase() : f.opp.toLowerCase())}</span>`;
const fxChips = (s, n) => parseFx(s).slice(0, n).map(fxSpan).join('');

/* ---------- Deadline ---------- */
let DEADLINE = null, GWNAME = '';
async function loadStatus() {
  try {
    const s = await (await api('/status')).json();
    const n = s.next_gameweek;
    if (!n) { $('#deadline').textContent = 'No upcoming gameweek'; return; }
    DEADLINE = new Date(n.deadline_time);
    GWNAME = n.name;
    tick();
    setInterval(tick, 1000);
  } catch (e) {
    $('#deadline').textContent = 'Deadline unavailable';
  }
}
function tick() {
  if (!DEADLINE) return;
  const ms = DEADLINE - Date.now();
  const ist = DEADLINE.toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true});
  if (ms <= 0) { $('#deadline').innerHTML = esc(GWNAME) + ' deadline passed (' + esc(ist) + ' IST)'; return; }
  const d = Math.floor(ms / 864e5), h = Math.floor(ms % 864e5 / 36e5), m = Math.floor(ms % 36e5 / 6e4), sec = Math.floor(ms % 6e4 / 1e3);
  $('#deadline').innerHTML = esc(GWNAME) + ' deadline: <b>' + esc(ist) + ' IST</b><br>' + (d ? d + 'd ' : '') + h + 'h ' + m + 'm ' + sec + 's left';
}

/* ---------- My Team ---------- */
const stat = (l, v, sub) => `<div class="stat"><span>${esc(l)}</span><b>${esc(v)}</b><small>${esc(sub || '')}</small></div>`;
const isFlagged = p => (p.chance_of_playing_next_round != null && p.chance_of_playing_next_round < 75) || (p.status !== 'a' && p.news);

function card(p) {
  const badge = p.captain ? '<b class="bdg c">C</b>' : (p.vice_captain ? '<b class="bdg v">V</b>' : '');
  return `<div class="card${isFlagged(p) ? ' flag' : ''}"><div class="nm">${esc(p.name)}${badge}</div>` +
    `<div class="sub">${esc(p.team)} · ${money(p.price)}</div>` +
    `<div class="ep"><span>xP <b>${p.expected_points_next}</b></span><span>Form <b>${p.form}</b></span></div>` +
    `<div class="fxs">${fxChips(p.next_fixtures, 4)}</div></div>`;
}

function renderTeam(t) {
  const chips = (t.chips_used || []).map(c => c.chip + ' (GW' + c.gameweek + ')').join(', ') || 'none yet';
  let html = '<div class="stats">' +
    stat('Team', t.team_name, t.manager) +
    stat('Overall rank', fmt(t.overall_rank), fmt(t.overall_points) + ' pts') +
    stat('Last GW', t.last_gw_points + ' pts', 'Squad shown: GW' + t.squad_gameweek) +
    stat('Bank', money(t.bank), 'Value ' + money(t.squad_value)) +
    stat('Transfers made', t.total_transfers, '') +
    stat('Chips used', chips, '') + '</div>';

  if (!t.squad) { $('#teamOut').innerHTML = html + '<p class="msg">Squad not available for this gameweek.</p>'; return; }

  const xi = t.squad.filter(p => p.slot !== 'bench');
  const bench = t.squad.filter(p => p.slot === 'bench');

  const flags = [];
  t.squad.forEach(p => {
    if (isFlagged(p)) {
      const c = p.chance_of_playing_next_round;
      flags.push(`${p.name} (${p.slot === 'bench' ? 'bench' : 'XI'}): ${p.news || 'flagged'}${c != null ? ' (' + c + '% to play)' : ''}`);
    }
  });
  const xiEp = xi.filter(p => p.position !== 'GK').map(p => p.expected_points_next);
  const minXi = xiEp.length ? Math.min(...xiEp) : 0;
  bench.forEach(p => {
    if (p.position === 'GK') return;
    if (p.expected_points_next < 1) flags.push(`${p.name} (bench): expects under 1 point, a dead slot`);
    else if (p.expected_points_next > minXi) flags.push(`${p.name} (bench): expects more than your weakest starter (${p.expected_points_next} vs ${minXi})`);
  });
  if (flags.length) html += '<div class="flags"><b>⚠ Check these</b><ul>' + flags.map(f => '<li>' + esc(f) + '</li>').join('') + '</ul></div>';

  html += '<div class="pitch">';
  ['GK', 'DEF', 'MID', 'FWD'].forEach(pos => {
    html += '<div class="row">' + xi.filter(p => p.position === pos).map(card).join('') + '</div>';
  });
  html += '</div>';
  html += '<div class="benchbox"><h3>BENCH (in order)</h3><div class="row">' + bench.map(card).join('') + '</div></div>';
  html += '<p class="legend">xP = FPL\'s expected points for the next gameweek. Chips show the next 4 fixtures. The squad is as locked at the last deadline.</p>';
  $('#teamOut').innerHTML = html;
}

async function loadTeam() {
  const id = $('#tid').value.trim();
  if (!/^\d+$/.test(id)) { $('#teamOut').innerHTML = '<p class="err">Enter your team ID (numbers only).</p>'; return; }
  try { localStorage.setItem('fpl_tid', id); } catch (e) {}
  $('#teamOut').innerHTML = WAIT;
  try { renderTeam(await (await api('/team/' + id)).json()); }
  catch (e) { $('#teamOut').innerHTML = '<p class="err">' + esc(e.message) + '</p>'; }
}

/* ---------- Transfers ---------- */
function playerLine(x, sell) {
  const price = sell ? 'sell £' + x.sell_price.toFixed(1) + 'm' : '£' + x.price.toFixed(1) + 'm';
  const note = (x.news && x.status !== 'a') ? ' ⚠ ' + esc(x.news) : '';
  return `<b>${esc(x.name)}</b> ${esc(x.team)} ${esc(x.position)}<br><span class="sub">${price} · proj ${x.projected_points} pts · form ${x.form}${note}</span><div class="fxs">${fxChips(x.next_fixtures, 4)}</div>`;
}
function moveHtml(m) {
  return `<div class="move"><div class="mv out"><small>SELL</small>${playerLine(m.out, true)}</div><div class="arrow">→</div>` +
    `<div class="mv in"><small>BUY</small>${playerLine(m.in, false)}</div><div class="gain">+${m.gain}</div></div>`;
}
function renderTransfers(d) {
  const r = d.recommendation;
  let h = `<div class="rec ${r.action === 'make' ? '' : 'roll'}"><h3>${r.action === 'make' ? '✅ Suggested move' : '⏸ Roll your transfer'}</h3><div>${esc(r.summary)}</div>`;
  if (r.action === 'make') {
    h += r.moves.map(moveHtml).join('');
    h += `<div class="sub">Gain +${r.gain} pts over ${d.horizon} GWs · hit −${r.hit_cost} · <b>net +${r.net_gain}</b></div>`;
  }
  h += '</div>';

  const row = (n, o) => o ? `<tr><td>${n}</td><td>${o.moves.map(m => esc(m.out.name) + ' → ' + esc(m.in.name)).join(', ')}</td><td>+${o.gain}</td><td>−${o.hit_cost}</td><td><b>${o.net_gain >= 0 ? '+' : ''}${o.net_gain}</b></td></tr>` : '';
  h += '<h3>Options compared</h3><div class="scroll"><table><tr><th>Option</th><th>Moves</th><th>Gain</th><th>Hit</th><th>Net</th></tr><tr><td>Roll</td><td>Keep your squad</td><td>0</td><td>0</td><td><b>0</b></td></tr>' +
       row('Best 1 transfer', d.options.single) + row('Best 2 transfers', d.options.double) + '</table></div>';

  h += '<h3>Who to sell, and the best replacements</h3>';
  if (!d.sell_candidates.length) h += '<p class="msg">No clear upgrades found within budget.</p>';
  d.sell_candidates.forEach(s => {
    h += `<div class="sell"><h4>${esc(s.player.name)} <span class="sub">${esc(s.player.team)} ${esc(s.player.position)} · sell £${s.player.sell_price.toFixed(1)}m · proj ${s.player.projected_points} pts</span></h4>` +
      (s.reasons.length ? `<div class="why">${s.reasons.map(esc).join(' · ')}</div>` : '') +
      '<div class="scroll"><table><tr><th>Replacement</th><th>Price</th><th>Proj</th><th>Gain</th><th>Fixtures</th></tr>' +
      s.alternatives.map(a => `<tr><td><b>${esc(a.in.name)}</b> ${esc(a.in.team)}</td><td>£${a.in.price.toFixed(1)}m</td><td>${a.in.projected_points}</td><td><b>+${a.gain}</b></td><td><div class="fxs" style="justify-content:flex-start">${fxChips(a.in.next_fixtures, 4)}</div></td></tr>`).join('') +
      '</table></div></div>';
  });
  h += '<p class="legend">' + d.assumptions.map(esc).join('<br>') + '</p>';
  $('#transfersOut').innerHTML = h;
}
async function loadTransfers() {
  const id = $('#tid').value.trim();
  if (!/^\d+$/.test(id)) { $('#transfersOut').innerHTML = '<p class="err">Enter your team ID on the My Team tab first.</p>'; return; }
  const q = new URLSearchParams({ft: $('#tFt').value, horizon: $('#tH').value});
  const bank = $('#tBank').value.trim();
  if (bank) q.set('bank', bank);
  $('#transfersOut').innerHTML = WAIT;
  try { renderTransfers(await (await api('/transfers/' + id + '?' + q.toString())).json()); }
  catch (e) { $('#transfersOut').innerHTML = '<p class="err">' + esc(e.message) + '</p>'; }
}

/* ---------- Players ---------- */
async function loadPlayers() {
  const q = new URLSearchParams({sort: $('#pSort').value, limit: $('#pLimit').value});
  if ($('#pPos').value) q.set('position', $('#pPos').value);
  const club = $('#pTeam').value.trim();
  if (club) q.set('team', club);
  $('#playersOut').innerHTML = WAIT;
  try {
    const d = await (await api('/players?' + q.toString())).json();
    if (!d.players.length) { $('#playersOut').innerHTML = '<p class="msg">No players found.</p>'; return; }
    let h = '<div class="scroll"><table><tr><th>Player</th><th>Pos</th><th>Team</th><th>Price</th><th>Form</th><th>xP next</th><th>Pts</th><th>Owned</th><th>Status</th></tr>';
    d.players.forEach(p => {
      const c = p.chance_of_playing_next_round;
      const st = isFlagged(p) ? (p.news || 'flagged') + (c != null ? ' (' + c + '%)' : '') : '';
      h += `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.position)}</td><td>${esc(p.team)}</td><td>${money(p.price)}</td><td>${p.form}</td><td>${p.expected_points_next}</td><td>${p.total_points}</td><td>${p.owned_by_percent}%</td><td>${esc(st)}</td></tr>`;
    });
    $('#playersOut').innerHTML = h + '</table></div>';
  } catch (e) { $('#playersOut').innerHTML = '<p class="err">' + esc(e.message) + '</p>'; }
}

/* ---------- Fixtures ---------- */
async function loadFixtures() {
  $('#fixturesOut').innerHTML = WAIT;
  try {
    const d = await (await api('/fixtures?n=6')).json();
    const gws = [];
    for (let i = 0; i < d.gameweeks_shown; i++) gws.push(d.from_gameweek + i);
    let h = '<div class="scroll"><table><tr><th>Team</th>' + gws.map(g => '<th>GW' + g + '</th>').join('') + '</tr>';
    d.teams.forEach(t => {
      const fx = parseFx(t.fixtures);
      h += '<tr><td><b>' + esc(t.team) + '</b></td>' + gws.map(g => {
        const list = fx.filter(f => f.gw === g);
        return list.length ? '<td>' + list.map(fxSpan).join(' ') + '</td>' : '<td class="blank">–</td>';
      }).join('') + '</tr>';
    });
    $('#fixturesOut').innerHTML = h + '</table></div>';
  } catch (e) { $('#fixturesOut').innerHTML = '<p class="err">' + esc(e.message) + '</p>'; }
}

/* ---------- For Claude ---------- */
async function copySnap() {
  const id = $('#tid').value.trim() || '__DEFAULT_TEAM__';
  $('#copyMsg').textContent = 'Loading…';
  try {
    const txt = await (await api('/snapshot/' + id)).text();
    $('#snapTxt').value = txt;
    try { await navigator.clipboard.writeText(txt); $('#copyMsg').textContent = 'Copied! Paste it into Claude.'; }
    catch (e) { $('#copyMsg').textContent = 'Select the text below and copy it.'; }
  } catch (e) { $('#copyMsg').textContent = e.message; }
}

/* ---------- Tabs and start-up ---------- */
const loaded = {};
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('main section').forEach(s => s.classList.toggle('on', s.id === b.dataset.tab));
  if (!loaded[b.dataset.tab]) {
    loaded[b.dataset.tab] = true;
    if (b.dataset.tab === 'transfers') loadTransfers();
    if (b.dataset.tab === 'players') loadPlayers();
    if (b.dataset.tab === 'fixtures') loadFixtures();
  }
}));
$('#loadTeam').addEventListener('click', loadTeam);
$('#loadPlayers').addEventListener('click', loadPlayers);
$('#loadTransfers').addEventListener('click', loadTransfers);
$('#copySnap').addEventListener('click', copySnap);
$('#tid').addEventListener('keydown', e => { if (e.key === 'Enter') loadTeam(); });
try { const saved = localStorage.getItem('fpl_tid'); if (saved) $('#tid').value = saved; } catch (e) {}
loadStatus();
loadTeam();
</script>
</body>
</html>
"""


@app.route("/")
@app.route("/app")
def dashboard():
    page = APP_HTML.replace("__DEFAULT_TEAM__", str(fpl.DEFAULT_TEAM_ID))
    return Response(page, mimetype="text/html; charset=utf-8")


@app.route("/api")
def home():
    return jsonify(
        name="FPL API",
        endpoints={
            "/status": "current and next gameweek",
            "/team/<id>": "manager summary + squad (?gw=)",
            "/transfers/<id>": "rule-based transfer suggestions (?ft=&horizon=&bank=)",
            "/players": "player list (?sort=&limit=&position=&team=)",
            "/fixtures": "fixture difficulty per team (?from_gw=&n=)",
            "/snapshot": "full readable report (text)",
            "/snapshot/<id>": "full readable report for a team ID (text)",
        },
        default_team_id=fpl.DEFAULT_TEAM_ID,
    )


@app.route("/status")
def status():
    _, _, events, current, upcoming = load_boot()
    return jsonify(
        current_gameweek=current and {k: current[k] for k in ("id", "name", "deadline_time", "finished", "average_entry_score", "highest_score")},
        next_gameweek=upcoming and {k: upcoming[k] for k in ("id", "name", "deadline_time")},
        gameweeks=[{"id": e["id"], "deadline": e["deadline_time"], "finished": e["finished"]} for e in events],
    )


@app.route("/team/<int:team_id>")
def team(team_id):
    players, teams, _, current, upcoming = load_boot()
    entry = fpl.get(f"entry/{team_id}/")
    if not entry:
        return jsonify(error="Team not found. Check the team ID."), 404

    gw = request.args.get("gw", type=int) or (current["id"] if current else (upcoming["id"] if upcoming else 1))
    fixtures = fpl.get("fixtures/") or []
    from_gw = upcoming["id"] if upcoming else gw
    history = fpl.get(f"entry/{team_id}/history/") or {}
    picks = fpl.get(f"entry/{team_id}/event/{gw}/picks/")

    squad = None
    gw_info = None
    if picks:
        gw_info = {**picks["entry_history"], "active_chip": picks.get("active_chip")}
        squad = []
        for pk in sorted(picks["picks"], key=lambda x: x["position"]):
            d = player_dict(players[pk["element"]], teams, fixtures, from_gw)
            d.update(slot="bench" if pk["position"] > 11 else "starting XI",
                     captain=pk["is_captain"], vice_captain=pk["is_vice_captain"])
            squad.append(d)

    return jsonify(
        team_id=team_id,
        team_name=entry["name"],
        manager=f"{entry['player_first_name']} {entry['player_last_name']}",
        overall_points=entry["summary_overall_points"],
        overall_rank=entry["summary_overall_rank"],
        last_gw_points=entry["summary_event_points"],
        bank=entry["last_deadline_bank"] / 10,
        squad_value=entry["last_deadline_value"] / 10,
        total_transfers=entry["last_deadline_total_transfers"],
        chips_used=[{"chip": c["name"], "gameweek": c["event"]} for c in history.get("chips", [])],
        squad_gameweek=gw,
        gameweek_info=gw_info,
        squad=squad,
        note="Squad is as locked at the deadline; later transfers are not visible in FPL's public data.",
    )


@app.route("/transfers/<int:team_id>")
def transfers_endpoint(team_id):
    ft = max(0, min(request.args.get("ft", 1, type=int), 5))
    horizon = max(1, min(request.args.get("horizon", 4, type=int), 8))
    players, teams, events, current, upcoming = load_boot()
    entry = fpl.get(f"entry/{team_id}/")
    if not entry:
        return jsonify(error="Team not found. Check the team ID."), 404
    gw = request.args.get("gw", type=int) or (current["id"] if current else (upcoming["id"] if upcoming else 1))
    picks = fpl.get(f"entry/{team_id}/event/{gw}/picks/")
    if not picks:
        return jsonify(error="Squad not available for this gameweek."), 404
    fixtures = fpl.get("fixtures/") or []
    transfers = fpl.get(f"entry/{team_id}/transfers/")
    first_gw = upcoming["id"] if upcoming else gw
    finished = sum(1 for e in events if e["finished"])
    bank_arg = request.args.get("bank", type=float)
    bank = int(round(bank_arg * 10)) if bank_arg is not None else entry["last_deadline_bank"]

    diffs = {tid: fixture_diffs(fixtures, tid, first_gw, horizon) for tid in teams}
    proj = {pid: project(p, diffs[p["team"]], first_gw, finished) for pid, p in players.items()}
    squad = [players[pk["element"]] for pk in picks["picks"]]
    squad_ids = {p["id"] for p in squad}
    sells = selling_prices(squad_ids, players, transfers)
    clubs = {}
    for p in squad:
        clubs[p["team"]] = clubs.get(p["team"], 0) + 1

    pool = {1: [], 2: [], 3: [], 4: []}
    for p in players.values():
        if p["id"] in squad_ids or p["status"] not in ("a", "d") or first_gw_availability(p) < 0.75:
            continue
        if finished >= 3 and p["minutes"] < 0.3 * 90 * finished:
            continue
        pool[p["element_type"]].append(p)

    moves = []  # (gain, out, in)
    for out in squad:
        budget = bank + sells[out["id"]]
        for c in pool[out["element_type"]]:
            if c["now_cost"] > budget:
                continue
            if c["team"] != out["team"] and clubs.get(c["team"], 0) >= 3:
                continue
            moves.append((round(proj[c["id"]] - proj[out["id"]], 1), out, c))
    moves.sort(key=lambda m: m[0], reverse=True)
    moves = [m for m in moves if m[0] > 0]

    def move_json(g, o, c):
        return {"gain": g,
                "out": brief(o, teams, fixtures, first_gw, horizon, proj[o["id"]], sells[o["id"]]),
                "in": brief(c, teams, fixtures, first_gw, horizon, proj[c["id"]])}

    def option(ms):
        gain = round(sum(m[0] for m in ms), 1)
        hit = HIT_COST * max(0, len(ms) - ft)
        return {"moves": [move_json(*m) for m in ms], "gain": gain, "hit_cost": hit, "net_gain": round(gain - hit, 1)}

    single = option([moves[0]]) if moves else None

    # best pair: top 4 replacements per player sold, then check budget and 3-per-club together
    by_out = {}
    for m in moves:
        lst = by_out.setdefault(m[1]["id"], [])
        if len(lst) < 4:
            lst.append(m)
    short = [m for lst in by_out.values() for m in lst]
    best_pair = None
    for i in range(len(short)):
        for j in range(i + 1, len(short)):
            g1, o1, c1 = short[i]
            g2, o2, c2 = short[j]
            if o1["id"] == o2["id"] or c1["id"] == c2["id"]:
                continue
            if c1["now_cost"] + c2["now_cost"] > bank + sells[o1["id"]] + sells[o2["id"]]:
                continue
            cnt = dict(clubs)
            for o in (o1, o2):
                cnt[o["team"]] -= 1
            for c in (c1, c2):
                cnt[c["team"]] = cnt.get(c["team"], 0) + 1
            if any(v > 3 for v in cnt.values()):
                continue
            if best_pair is None or g1 + g2 > best_pair[0]:
                best_pair = (g1 + g2, [short[i], short[j]])
    double = option(best_pair[1]) if best_pair else None

    best = max([o for o in (single, double) if o], key=lambda o: o["net_gain"], default=None)
    if best and best["net_gain"] >= MIN_NET_GAIN:
        parts = [f"Sell {m['out']['name']} (£{m['out']['sell_price']:.1f}m), buy {m['in']['name']} (£{m['in']['price']:.1f}m)" for m in best["moves"]]
        if best["hit_cost"]:
            hit_txt = f"costs a hit of {best['hit_cost']} points"
        else:
            hit_txt = "uses your free transfers" if len(best["moves"]) > 1 else "uses your free transfer"
        rec = {"action": "make", "summary": "; ".join(parts) + f". Projected +{best['gain']} pts over {horizon} GWs; {hit_txt}.",
               **{k: best[k] for k in ("moves", "gain", "hit_cost", "net_gain")}}
    else:
        top = f" The best available move gains only +{best['net_gain']} net." if best else ""
        rec = {"action": "roll", "moves": [], "gain": 0, "hit_cost": 0, "net_gain": 0,
               "summary": f"Nothing clears the +{MIN_NET_GAIN:g} point bar over {horizon} GWs, so hold your squad and keep the free transfer.{top}"}

    sell_candidates = []
    for pid, lst in by_out.items():
        o = lst[0][1]
        reasons = []
        if first_gw_availability(o) < 0.75:
            reasons.append("Availability concern" + (": " + o["news"] if o.get("news") else ""))
        d_list = diffs[o["team"]]
        if not d_list:
            reasons.append("No fixtures in this window")
        elif sum(d for _, d in d_list) / len(d_list) >= 3.6:
            reasons.append("Tough fixtures")
        if float(o["form"]) < 3:
            reasons.append("Poor form")
        sell_candidates.append(((first_gw_availability(o) < 0.75, lst[0][0]), {"player": brief(o, teams, fixtures, first_gw, horizon, proj[pid], sells[pid]),
                                            "reasons": reasons,
                                            "alternatives": [{"gain": g, "in": brief(c, teams, fixtures, first_gw, horizon, proj[c["id"]])} for g, _, c in lst[:3]]}))
    sell_candidates.sort(key=lambda x: x[0], reverse=True)

    return jsonify(
        team_id=team_id,
        team_name=entry["name"],
        gameweeks=[first_gw + i for i in range(horizon)],
        horizon=horizon,
        free_transfers=ft,
        bank=bank / 10,
        recommendation=rec,
        options={"single": single, "double": double},
        sell_candidates=[s for _, s in sell_candidates[:6]],
        assumptions=[
            "Projections are a simple rule-based estimate (form, FPL expected points, points per game, fixture difficulty, availability), not an official FPL forecast.",
            "Selling prices are estimated from your transfer history using FPL's keep-half-the-profit rule.",
            "Squad and bank are as locked at the last deadline. Set your real free transfers and bank if they differ.",
            f"A move is only suggested if it nets at least +{MIN_NET_GAIN:g} points after any {HIT_COST}-point hit. Rolling a free transfer also has value.",
        ],
    )


SORT_FIELDS = {
    "form": "form", "total_points": "total_points", "ep_next": "expected_points_next",
    "price": "price", "owned": "owned_by_percent", "transfers_in": "transfers_in_this_gw",
    "points_per_game": "points_per_game", "xgi": "xGI",
}


@app.route("/players")
def players_endpoint():
    players, teams, _, _, upcoming = load_boot()
    sort = request.args.get("sort", "form")
    if sort not in SORT_FIELDS:
        return jsonify(error=f"sort must be one of: {', '.join(SORT_FIELDS)}"), 400
    limit = min(request.args.get("limit", 20, type=int), 100)
    pos = request.args.get("position", "").upper()
    team_code = request.args.get("team", "").upper()

    rows = [player_dict(p, teams) for p in players.values() if p["minutes"] > 0 or sort == "transfers_in"]
    if pos:
        rows = [r for r in rows if r["position"] == pos]
    if team_code:
        rows = [r for r in rows if r["team"] == team_code]
    rows.sort(key=lambda r: r[SORT_FIELDS[sort]], reverse=True)
    return jsonify(count=len(rows[:limit]), sorted_by=sort, players=rows[:limit])


@app.route("/fixtures")
def fixtures_endpoint():
    _, teams, _, current, upcoming = load_boot()
    fixtures = fpl.get("fixtures/") or []
    default_from = upcoming["id"] if upcoming else (current["id"] if current else 1)
    from_gw = request.args.get("from_gw", default_from, type=int)
    n = request.args.get("n", fpl.LOOKAHEAD, type=int)
    return jsonify(
        from_gameweek=from_gw,
        gameweeks_shown=n,
        note="Number in [] is FPL's difficulty rating: 1 = easy, 5 = hard.",
        teams=[{"team": t["name"], "short": t["short_name"],
                "fixtures": fpl.fixtures_text(fixtures, teams, t["id"], from_gw, n)}
               for t in sorted(teams.values(), key=lambda t: t["name"])],
    )


@app.route("/snapshot")
@app.route("/snapshot/<int:team_id>")
def snapshot(team_id=None):
    text = fpl.build_snapshot(team_id or fpl.DEFAULT_TEAM_ID, request.args.get("gw", type=int))
    return Response(text, mimetype="text/markdown; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
