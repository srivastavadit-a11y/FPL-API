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
<meta name="theme-color" content="#37003c">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='30' fill='%2337003c'/%3E%3Ccircle cx='32' cy='32' r='14' fill='%2300ff85'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=Manrope:wght@400..800&display=swap" rel="stylesheet">
<title>FPL Dashboard</title>
<style>
:root{
  --violet:#37003c;--plum:#1d0023;--pink:#e90052;--pinksoft:#ff8fb5;--cyan:#04f5ff;--green:#00ff85;
  --ink:#fbf7ff;--mute:#cbb8db;--line:rgba(255,255,255,.14);--glass:rgba(255,255,255,.07);
  --display:"Bricolage Grotesque","Avenir Next","Segoe UI",system-ui,sans-serif;
  --body:Manrope,"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;font:15px/1.55 var(--body);color:var(--ink);background:var(--plum);min-height:100vh;-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-1;background:
  radial-gradient(60vmax 46vmax at 8% -8%,rgba(233,0,82,.55),transparent 62%),
  radial-gradient(52vmax 42vmax at 104% 6%,rgba(4,245,255,.26),transparent 62%),
  radial-gradient(62vmax 48vmax at 55% 112%,rgba(0,255,133,.20),transparent 62%),
  linear-gradient(180deg,#2c0033 0%,#37003c 48%,#1d0023 100%)}
:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px 56px}

/* top and hero */
.top{display:flex;align-items:center;padding:20px 0 0}
.brand{display:flex;align-items:center;gap:10px;font:700 19px var(--display);letter-spacing:.2px}
.mark{width:26px;height:26px;border-radius:50%;background:conic-gradient(from 200deg,#e90052,#04f5ff,#00ff85,#e90052);box-shadow:0 0 0 3px rgba(255,255,255,.12)}
.hero{padding:22px 0 24px}
.gwname{margin:0;color:var(--mute);font-size:14px}
.count{display:flex;flex-wrap:wrap;gap:6px clamp(14px,4vw,34px);margin:6px 0 8px}
.seg{display:flex;flex-direction:column}
.seg b{font:800 clamp(38px,10vw,64px)/1 var(--display);font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.seg:last-child b{color:var(--cyan)}
.seg i{font-style:normal;font-size:12.5px;color:var(--mute);margin-top:2px}
.when{margin:0;font-weight:600}
.passed{font:700 30px var(--display)}

/* tabs */
nav{position:sticky;top:8px;z-index:5;display:flex;gap:4px;padding:5px;width:fit-content;max-width:100%;overflow-x:auto;
  background:rgba(24,0,30,.62);border:1px solid var(--line);border-radius:999px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 10px 30px -12px rgba(0,0,0,.6)}
nav button{border:0;background:transparent;color:var(--mute);padding:9px 18px;border-radius:999px;font:600 14px var(--body);cursor:pointer;white-space:nowrap;transition:background .2s,color .2s}
nav button:hover{color:var(--ink)}
nav button.on{background:linear-gradient(135deg,var(--green),var(--cyan));color:var(--violet);font-weight:700}
main{margin-top:20px}
section{display:none}
section.on{display:block}

/* surfaces */
.panel{background:var(--glass);border:1px solid var(--line);border-radius:20px;padding:16px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 20px 44px -26px rgba(0,0,0,.7)}
@supports not (backdrop-filter:blur(1px)){.panel{background:rgba(52,12,64,.9)}}
h2,h3,h4{font-family:var(--display);margin:0;letter-spacing:.1px}
h3{font-size:19px;margin:26px 0 12px}
.legend{font-size:12.5px;color:var(--mute);margin:10px 2px}
.msg{color:var(--mute);padding:14px 2px}
.err{background:rgba(233,0,82,.16);border:1px solid rgba(233,0,82,.5);color:#ffd6e5;padding:12px 14px;border-radius:14px}

/* controls */
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:16px}
.bar label{color:var(--mute);font-size:14px}
input,select{font:inherit;color:var(--ink);padding:9px 12px;border:1px solid rgba(255,255,255,.2);border-radius:12px;background:rgba(255,255,255,.08)}
input{width:120px}
input::placeholder{color:#bba7cc}
select option{color:#1d0023;background:#fff}
button.act{font:700 14px var(--body);padding:10px 18px;border:0;border-radius:999px;background:linear-gradient(135deg,var(--green),var(--cyan));color:var(--violet);cursor:pointer;transition:filter .2s}
button.act:hover{filter:brightness(.92)}

/* summary */
.summary{padding:20px 20px 16px}
.who h2{font:800 clamp(26px,5.5vw,38px)/1.1 var(--display)}
.who p{margin:4px 0 18px;color:var(--mute)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:16px}
.stat span{display:block;color:var(--mute);font-size:13px}
.stat b{display:block;font:700 23px/1.25 var(--display);font-variant-numeric:tabular-nums}
.stat small{color:var(--mute);font-size:12px}
.chipsline{margin:16px 0 0;color:var(--mute);font-size:13.5px}

.flags{background:rgba(233,0,82,.14);border:1px solid rgba(233,0,82,.45);border-radius:16px;padding:14px 18px;margin:16px 0;color:#ffd6e5}
.flags b{color:#fff;font-family:var(--display);font-size:16px}
.flags ul{margin:6px 0 0;padding-left:18px}
.flags li{margin:2px 0}

/* pitch */
.pitch{position:relative;max-width:780px;margin-inline:auto;border-radius:24px;padding:22px 8px 20px;overflow:hidden;border:1px solid rgba(255,255,255,.2);
  background:linear-gradient(180deg,rgba(55,0,60,.35),transparent 28%),repeating-linear-gradient(180deg,#0f7b47 0 46px,#0b6a3d 46px 92px);
  box-shadow:inset 0 0 70px rgba(0,0,0,.28),0 26px 54px -26px rgba(0,0,0,.75)}
.pitch::before{content:"";position:absolute;inset:10px;border:2px solid rgba(255,255,255,.26);border-radius:14px;pointer-events:none}
.pitch .half{position:absolute;left:10px;right:10px;top:50%;height:2px;background:rgba(255,255,255,.2);pointer-events:none}
.pitch .circle{position:absolute;left:50%;top:50%;width:96px;height:96px;border:2px solid rgba(255,255,255,.2);border-radius:50%;transform:translate(-50%,-50%);pointer-events:none}
.pitch .pbox{position:absolute;left:50%;width:46%;height:15%;border:2px solid rgba(255,255,255,.22);transform:translateX(-50%);pointer-events:none}
.pitch .pbox.t{top:10px;border-top:0}
.pitch .pbox.b{bottom:10px;border-bottom:0}
.row{display:flex;justify-content:center;gap:8px;position:relative;z-index:1}
.pitch .row{margin-bottom:22px}
.pitch .row:last-of-type{margin-bottom:6px}

.card{flex:0 1 108px;min-width:0;text-align:center;position:relative}
.card .fx{font-size:10px;padding:2px 4px}
.kit{position:relative;width:50px;height:50px;margin:0 auto -7px;filter:drop-shadow(0 6px 6px rgba(0,0,0,.35))}
.shirt{display:block;width:100%;height:100%}
.bdg{position:absolute;top:-3px;right:-5px;width:19px;height:19px;border-radius:50%;font:800 11px/19px var(--body);text-align:center;box-shadow:0 2px 6px rgba(0,0,0,.4)}
.bdg.c{background:var(--pink);color:#fff}
.bdg.v{background:var(--cyan);color:var(--violet)}
.plate{position:relative;background:#fff;color:var(--violet);border-radius:9px;overflow:hidden;box-shadow:0 8px 16px -8px rgba(0,0,0,.6)}
.plate .nm{display:block;padding:3px 5px;font:700 11.5px/1.25 var(--body);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.plate .pt{display:block;background:var(--green);color:var(--violet);font:800 11.5px/1.55 var(--body);font-variant-numeric:tabular-nums}
.card.flag .pt{background:var(--pink);color:#fff}
.meta{font-size:11px;color:rgba(255,255,255,.88);margin-top:4px}
.card .fxs{margin-top:3px}
.ord{position:absolute;left:0;top:0;width:19px;height:19px;border-radius:50%;background:rgba(255,255,255,.18);font:700 11px/19px var(--body)}
.benchbox{max-width:780px;margin:16px auto 0;padding:14px 8px 16px}
.benchbox h3{margin:0 0 12px 10px;font-size:15px;color:var(--mute)}
.benchbox .row{flex-wrap:nowrap}

/* fixtures chips (difficulty 1 easy to 5 hard) */
.fxs{display:flex;gap:3px;justify-content:center;flex-wrap:wrap}
.fx{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 5px;border-radius:6px;font-variant-numeric:tabular-nums}
.d1{background:#00ff85;color:#1d0023}.d2{background:#8ef0bd;color:#1d0023}.d3{background:#dcd3e6;color:#1d0023}.d4{background:#ff86ad;color:#1d0023}.d5{background:#e90052;color:#fff}

/* tables */
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.09);text-align:left;white-space:nowrap}
th{color:var(--mute);font-size:12.5px;font-weight:600}
tr:last-child td{border-bottom:0}
tbody tr:hover td,table tr:hover td{background:rgba(255,255,255,.04)}
td.blank{color:var(--mute);text-align:center}
@media (max-width:700px){.sticky td:first-child,.sticky th:first-child{position:sticky;left:0;background:#2c0836;z-index:1}}
.tn{display:flex;align-items:center;gap:8px}
.tn .shirt{width:22px;height:22px}

/* transfers */
.rec{padding:20px;border-color:rgba(0,255,133,.55);box-shadow:0 0 0 4px rgba(0,255,133,.08),0 24px 50px -26px rgba(0,0,0,.7)}
.rec.roll{border-color:var(--line);box-shadow:0 20px 44px -26px rgba(0,0,0,.7)}
.rec h3{margin:0 0 6px;display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
.roll .dot{background:var(--cyan);box-shadow:0 0 10px var(--cyan)}
.move{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin:14px 0;padding:14px;border-radius:16px;background:rgba(0,0,0,.2);border:1px solid var(--line)}
.mv{flex:1 1 210px;display:flex;gap:10px;align-items:flex-start}
.mini{flex:0 0 38px}
.mini .shirt{width:38px;height:38px}
.txt small{display:block;font-weight:700;font-size:12.5px}
.mv.out small{color:var(--pinksoft)}.mv.in small{color:var(--green)}
.txt .sub,.sub{color:var(--mute);font-size:12.5px}
.txt .fxs{justify-content:flex-start;margin-top:4px}
.arrow{font-size:20px;color:var(--mute)}
.gain{font:800 24px var(--display);color:var(--green);min-width:64px;text-align:right}
.sell{margin:14px 0;padding:16px}
.sell h4{font-size:17px;margin-bottom:2px}
.why{font-size:13px;color:var(--pinksoft);margin:2px 0 8px}

textarea{width:100%;height:280px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--ink);background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:14px;padding:12px}

/* one orchestrated moment: the squad settles onto the pitch */
@keyframes settle{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.intro .pitch .card{animation:settle .55s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(var(--i) * 40ms)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

@media (max-width:480px){
  .wrap{padding-inline:12px}
  nav{gap:2px}
  nav button{padding:8px 8px;font-size:13px}
  .kit{width:42px;height:42px}
  .plate .nm{font-size:10px;padding:3px 3px}
  .plate .pt{font-size:10.5px}
  .card .fxs .fx:nth-child(n+3){display:none}
  .pitch{padding-inline:4px}
  .row{gap:4px}
  .summary{padding:16px}
}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>
  <symbol id="shirt-body" viewBox="0 0 64 64">
    <path d="M22 6 L6 14 L2 30 L12 34 L14 28 L14 58 L50 58 L50 28 L52 34 L62 30 L58 14 L42 6 C40 12 36 14 32 14 C28 14 24 12 22 6 Z" fill="var(--k)" stroke="rgba(0,0,0,.38)" stroke-width="1.6" stroke-linejoin="round"/>
    <path d="M22 6 C24 12 28 14 32 14 C36 14 40 12 42 6" fill="none" stroke="var(--t)" stroke-width="3" stroke-linecap="round"/>
    <path d="M3.5 29.5 L12.5 33.5 M60.5 29.5 L51.5 33.5" stroke="var(--t)" stroke-width="3.2" stroke-linecap="round"/>
  </symbol>
</defs></svg>

<div class="wrap">
  <div class="top"><div class="brand"><span class="mark"></span><span>FPL Dashboard</span></div></div>

  <div id="deadline" class="hero">
    <p id="gwName" class="gwname">Next deadline</p>
    <div id="count" class="count"><span class="passed">Loading…</span></div>
    <p id="when" class="when"></p>
  </div>

  <nav id="tabs">
    <button class="on" data-tab="team">My Team</button>
    <button data-tab="transfers">Transfers</button>
    <button data-tab="players">Players</button>
    <button data-tab="fixtures">Fixtures</button>
    <button data-tab="claude">Claude</button>
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
        <input id="tBank" placeholder="Bank £m (auto)" style="width:140px">
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
        <input id="pTeam" placeholder="Club, e.g. ARS" maxlength="3" style="width:140px">
        <select id="pLimit"><option>20</option><option>40</option><option>100</option></select>
        <button class="act" id="loadPlayers">Show players</button>
      </div>
      <div id="playersOut"></div>
    </section>

    <section id="fixtures">
      <p class="legend">Colour shows FPL's difficulty rating from 1 (easy) to 5 (hard). Capitals are home games, lowercase are away.</p>
      <div id="fixturesOut"></div>
    </section>

    <section id="claude">
      <p>Copy the full report and paste it into any Claude chat to get advice, for example when your project can't open the link.</p>
      <div class="bar"><button class="act" id="copySnap">Copy report for Claude</button><span id="copyMsg" class="msg"></span></div>
      <textarea id="snapTxt" readonly placeholder="The report will appear here"></textarea>
    </section>
  </main>
</div>

<script>
const $ = s => document.querySelector(s);
const KEY = new URLSearchParams(location.search).get('key');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => '£' + Number(n).toFixed(1) + 'm';
const fmt = n => Number(n).toLocaleString('en-IN');
const pad = n => String(n).padStart(2, '0');
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

/* ---------- Club shirts ---------- */
const CLUB = {
  ARS:['#ef0107','#ffffff'], AVL:['#7a003c','#95bfe5'], BOU:['#da291c','#111111'], BRE:['#e30613','#ffffff'],
  BHA:['#0057b8','#ffffff'], BUR:['#6c1d45','#99d6ea'], CHE:['#034694','#ffffff'], CRY:['#1b458f','#c4122e'],
  EVE:['#003399','#ffffff'], FUL:['#ffffff','#111111'], LEE:['#ffffff','#1d428a'], LIV:['#c8102e','#ffffff'],
  MCI:['#6cabdd','#ffffff'], MUN:['#da291c','#ffffff'], NEW:['#2b2b2b','#ffffff'], NFO:['#dd0000','#ffffff'],
  SUN:['#eb172b','#ffffff'], TOT:['#ffffff','#132257'], WHU:['#7a263a','#1bb1e7'], WOL:['#fdb913','#231f20']
};
function shirt(code) {
  const c = CLUB[code] || ['#6d2a96', '#ffffff'];
  return `<svg class="shirt" viewBox="0 0 64 64" style="--k:${c[0]};--t:${c[1]}" aria-hidden="true"><use href="#shirt-body"/></svg>`;
}

/* ---------- Fixtures helpers ---------- */
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
    if (!n) { $('#count').innerHTML = '<span class="passed">No upcoming gameweek</span>'; $('#gwName').textContent = 'Season'; return; }
    DEADLINE = new Date(n.deadline_time);
    GWNAME = n.name;
    tick();
    setInterval(tick, 1000);
  } catch (e) {
    $('#count').innerHTML = '<span class="passed">Deadline unavailable</span>';
  }
}
const seg = (v, l) => `<span class="seg"><b>${pad(v)}</b><i>${l}</i></span>`;
function tick() {
  if (!DEADLINE) return;
  const ms = DEADLINE - Date.now();
  const ist = DEADLINE.toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true});
  $('#gwName').textContent = GWNAME + ' deadline';
  $('#when').textContent = ist + ' IST';
  if (ms <= 0) { $('#count').innerHTML = '<span class="passed">Deadline passed</span>'; return; }
  const d = Math.floor(ms / 864e5), h = Math.floor(ms % 864e5 / 36e5), m = Math.floor(ms % 36e5 / 6e4), s = Math.floor(ms % 6e4 / 1e3);
  $('#count').innerHTML = (d ? seg(d, d === 1 ? 'day' : 'days') : '') + seg(h, 'hours') + seg(m, 'minutes') + seg(s, 'seconds');
}

/* ---------- My Team ---------- */
const stat = (l, v, sub) => `<div class="stat"><span>${esc(l)}</span><b>${esc(v)}</b>${sub ? '<small>' + esc(sub) + '</small>' : ''}</div>`;
const isFlagged = p => (p.chance_of_playing_next_round != null && p.chance_of_playing_next_round < 75) || (p.status !== 'a' && p.news);

function card(p, i, ord) {
  const flag = isFlagged(p);
  const c = p.chance_of_playing_next_round;
  const badge = p.captain ? '<b class="bdg c">C</b>' : (p.vice_captain ? '<b class="bdg v">V</b>' : '');
  const strip = flag ? (c != null ? (c === 0 ? 'Out' : c + '%') : 'Doubt') : p.expected_points_next + ' xP';
  const tip = `${p.name}: form ${p.form}, ${money(p.price)}${p.news ? ', ' + p.news : ''}`;
  return `<div class="card${flag ? ' flag' : ''}" style="--i:${i}" title="${esc(tip)}">` +
    (ord ? `<span class="ord">${ord}</span>` : '') +
    `<div class="kit">${shirt(p.team)}${badge}</div>` +
    `<div class="plate"><span class="nm">${esc(p.name)}</span><span class="pt">${esc(strip)}</span></div>` +
    `<div class="meta">${money(p.price)}</div>` +
    `<div class="fxs">${fxChips(p.next_fixtures, 3)}</div></div>`;
}

function renderTeam(t) {
  const chips = (t.chips_used || []).map(c => c.chip + ' (GW' + c.gameweek + ')').join(', ') || 'none yet';
  let html = '<div class="panel summary"><div class="who"><h2>' + esc(t.team_name) + '</h2><p>' + esc(t.manager) + '</p></div><div class="stats">' +
    stat('Overall rank', fmt(t.overall_rank)) +
    stat('Total points', fmt(t.overall_points)) +
    stat('Last gameweek', t.last_gw_points + ' pts', 'GW' + t.squad_gameweek + ' squad') +
    stat('Bank', money(t.bank)) +
    stat('Squad value', money(t.squad_value)) +
    stat('Transfers made', t.total_transfers) +
    '</div><p class="chipsline">Chips used: ' + esc(chips) + '</p></div>';

  if (!t.squad) { $('#teamOut').innerHTML = html + '<p class="msg">Squad not available for this gameweek.</p>'; return; }

  const xi = t.squad.filter(p => p.slot !== 'bench');
  const bench = t.squad.filter(p => p.slot === 'bench');

  const flags = [];
  t.squad.forEach(p => {
    if (isFlagged(p)) {
      const c = p.chance_of_playing_next_round;
      flags.push(`${p.name} (${p.slot === 'bench' ? 'bench' : 'starting XI'}): ${p.news || 'flagged'}${c != null ? ', ' + c + '% to play' : ''}`);
    }
  });
  const xiEp = xi.filter(p => p.position !== 'GK').map(p => p.expected_points_next);
  const minXi = xiEp.length ? Math.min(...xiEp) : 0;
  bench.forEach(p => {
    if (p.position === 'GK') return;
    if (p.expected_points_next < 1) flags.push(`${p.name} (bench): expects under 1 point, a dead slot`);
    else if (p.expected_points_next > minXi) flags.push(`${p.name} (bench): expects more than your weakest starter (${p.expected_points_next} vs ${minXi})`);
  });
  if (flags.length) html += '<div class="flags"><b>Worth a look before the deadline</b><ul>' + flags.map(f => '<li>' + esc(f) + '</li>').join('') + '</ul></div>';

  let n = 0;
  html += '<div class="pitch"><i class="half"></i><i class="circle"></i><i class="pbox t"></i><i class="pbox b"></i>';
  ['GK', 'DEF', 'MID', 'FWD'].forEach(pos => {
    html += '<div class="row">' + xi.filter(p => p.position === pos).map(p => card(p, n++)).join('') + '</div>';
  });
  html += '</div>';
  html += '<div class="panel benchbox"><h3>Bench, in order</h3><div class="row">' + bench.map((p, k) => card(p, n++, k + 1)).join('') + '</div></div>';
  html += '<p class="legend">The green strip is FPL\'s expected points for the next gameweek. A pink strip shows the chance of playing. Chips show the next fixtures. The squad is as locked at the last deadline.</p>';
  const out = $('#teamOut');
  out.classList.add('intro');
  out.innerHTML = html;
}

async function loadTeam() {
  const id = $('#tid').value.trim();
  if (!/^\d+$/.test(id)) { $('#teamOut').innerHTML = '<p class="err">Enter your team ID using numbers only.</p>'; return; }
  try { localStorage.setItem('fpl_tid', id); } catch (e) {}
  $('#teamOut').classList.remove('intro');
  $('#teamOut').innerHTML = WAIT;
  try { renderTeam(await (await api('/team/' + id)).json()); }
  catch (e) { $('#teamOut').innerHTML = '<p class="err">' + esc(e.message) + '</p>'; }
}

/* ---------- Transfers ---------- */
function playerLine(x, sell, label) {
  const price = sell ? 'sell at £' + x.sell_price.toFixed(1) + 'm' : '£' + x.price.toFixed(1) + 'm';
  const note = (x.news && x.status !== 'a') ? ', ' + esc(x.news) : '';
  return `<span class="mini">${shirt(x.team)}</span><div class="txt"><small>${label}</small><b>${esc(x.name)}</b> <span class="sub">${esc(x.team)} ${esc(x.position)}</span><br>` +
    `<span class="sub">${price}, projected ${x.projected_points} pts, form ${x.form}${note}</span><div class="fxs">${fxChips(x.next_fixtures, 4)}</div></div>`;
}
function moveHtml(m) {
  return `<div class="move"><div class="mv out">${playerLine(m.out, true, 'Sell')}</div><div class="arrow">→</div>` +
    `<div class="mv in">${playerLine(m.in, false, 'Buy')}</div><div class="gain">+${m.gain}</div></div>`;
}
function renderTransfers(d) {
  const r = d.recommendation;
  let h = `<div class="panel rec ${r.action === 'make' ? '' : 'roll'}"><h3><span class="dot"></span>${r.action === 'make' ? 'Suggested move' : 'Roll your transfer'}</h3><div>${esc(r.summary)}</div>`;
  if (r.action === 'make') {
    h += r.moves.map(moveHtml).join('');
    h += `<div class="sub">Gain +${r.gain} pts over ${d.horizon} GWs, hit ${r.hit_cost ? '−' + r.hit_cost : 'none'}, <b>net +${r.net_gain}</b></div>`;
  }
  h += '</div>';

  const row = (n, o) => o ? `<tr><td>${n}</td><td>${o.moves.map(m => esc(m.out.name) + ' → ' + esc(m.in.name)).join(', ')}</td><td>+${o.gain}</td><td>${o.hit_cost ? '−' + o.hit_cost : '0'}</td><td><b>${o.net_gain >= 0 ? '+' : ''}${o.net_gain}</b></td></tr>` : '';
  h += '<h3>Options compared</h3><div class="panel scroll"><table><tr><th>Option</th><th>Moves</th><th>Gain</th><th>Hit</th><th>Net</th></tr><tr><td>Roll</td><td>Keep your squad</td><td>0</td><td>0</td><td><b>0</b></td></tr>' +
       row('Best 1 transfer', d.options.single) + row('Best 2 transfers', d.options.double) + '</table></div>';

  h += '<h3>Who to sell and the best replacements</h3>';
  if (!d.sell_candidates.length) h += '<p class="msg">No clear upgrades found within budget.</p>';
  d.sell_candidates.forEach(s => {
    h += `<div class="panel sell"><h4>${esc(s.player.name)} <span class="sub">${esc(s.player.team)} ${esc(s.player.position)}, sell at £${s.player.sell_price.toFixed(1)}m, projected ${s.player.projected_points} pts</span></h4>` +
      (s.reasons.length ? `<div class="why">${s.reasons.map(esc).join(', ')}</div>` : '') +
      '<div class="scroll"><table><tr><th>Replacement</th><th>Price</th><th>Proj</th><th>Gain</th><th>Fixtures</th></tr>' +
      s.alternatives.map(a => `<tr><td><div class="tn">${shirt(a.in.team)}<span><b>${esc(a.in.name)}</b> <span class="sub">${esc(a.in.team)}</span></span></div></td><td>£${a.in.price.toFixed(1)}m</td><td>${a.in.projected_points}</td><td><b>+${a.gain}</b></td><td><div class="fxs" style="justify-content:flex-start">${fxChips(a.in.next_fixtures, 4)}</div></td></tr>`).join('') +
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
    if (!d.players.length) { $('#playersOut').innerHTML = '<p class="msg">No players found. Try another club code or position.</p>'; return; }
    let h = '<div class="panel scroll"><table><tr><th>Player</th><th>Pos</th><th>Price</th><th>Form</th><th>xP next</th><th>Pts</th><th>Owned</th><th>Status</th></tr>';
    d.players.forEach(p => {
      const c = p.chance_of_playing_next_round;
      const st = isFlagged(p) ? (p.news || 'flagged') + (c != null ? ' (' + c + '%)' : '') : '';
      h += `<tr><td><div class="tn">${shirt(p.team)}<span><b>${esc(p.name)}</b> <span class="sub">${esc(p.team)}</span></span></div></td><td>${esc(p.position)}</td><td>${money(p.price)}</td><td>${p.form}</td><td>${p.expected_points_next}</td><td>${p.total_points}</td><td>${p.owned_by_percent}%</td><td>${esc(st)}</td></tr>`;
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
    let h = '<div class="panel scroll"><table class="sticky"><tr><th>Team</th>' + gws.map(g => '<th>GW' + g + '</th>').join('') + '</tr>';
    d.teams.forEach(t => {
      const fx = parseFx(t.fixtures);
      h += '<tr><td><div class="tn">' + shirt(t.short) + '<b>' + esc(t.team) + '</b></div></td>' + gws.map(g => {
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
    try { await navigator.clipboard.writeText(txt); $('#copyMsg').textContent = 'Copied. Paste it into Claude.'; }
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
