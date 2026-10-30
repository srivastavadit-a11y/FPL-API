"""
FPL API - a small web service that returns live Fantasy Premier League data.

Needs fpl_fetch.py in the same folder (it reuses that file's code).

Endpoints (all return JSON unless noted):
    /                      list of endpoints
    /status                current + next gameweek and deadlines
    /team/<id>             a manager's summary and squad  (optional ?gw=10)
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


# ---- Endpoints ----
@app.route("/")
def home():
    return jsonify(
        name="FPL API",
        endpoints={
            "/status": "current and next gameweek",
            "/team/<id>": "manager summary + squad (?gw=)",
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
