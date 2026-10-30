#!/usr/bin/env python3
"""
FPL Snapshot - fetches live Fantasy Premier League data and saves it as one
easy-to-read file (fpl_snapshot.md) that you can upload to a Claude Project.

No installs needed: it only uses Python's built-in tools.

HOW TO RUN (in a terminal / command prompt, in the folder with this file):
    python fpl_fetch.py                  -> uses your team ID below, current gameweek
    python fpl_fetch.py 123456           -> a different team ID
    python fpl_fetch.py --gw 10          -> look at your squad as it was in gameweek 10
    python fpl_fetch.py --out my.md      -> save under a different file name

(On Mac/Linux you may need to type python3 instead of python.)
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---- EDIT THIS if you want a different default team -------------------------
DEFAULT_TEAM_ID = 162069
# -----------------------------------------------------------------------------

BASE = "https://fantasy.premierleague.com/api/"
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
LOOKAHEAD = 5  # how many gameweeks of fixtures to show


def get(path, retries=3):
    """Download one page of FPL data. Returns None if it doesn't exist (404)."""
    req = Request(BASE + path, headers={"User-Agent": "Mozilla/5.0 (FPL snapshot script)"})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            # 429 / 503 happen when FPL is busy or updating - wait and retry
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(
                f"FPL returned error {e.code} for '{path}'. "
                "If it says 503, the game is probably updating - try again in a few minutes."
            )
        except (URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise RuntimeError(f"Could not reach FPL ({e}). Check your internet connection.")
    return None


def money(tenths):
    return f"£{tenths / 10:.1f}m"


def fixtures_text(fixtures, teams, team_id, from_gw, n=LOOKAHEAD):
    """e.g. 'GW12 ARS(H)[4], GW13 CHE(A)[3]' - number in [] is FPL difficulty 1-5."""
    found = []
    for f in fixtures:
        gw = f.get("event")
        if gw is None or gw < from_gw or gw >= from_gw + n:
            continue
        if f["team_h"] == team_id:
            found.append((gw, f"{teams[f['team_a']]['short_name']}(H)[{f['team_h_difficulty']}]"))
        elif f["team_a"] == team_id:
            found.append((gw, f"{teams[f['team_h']]['short_name']}(A)[{f['team_a_difficulty']}]"))
    found.sort()
    return ", ".join(f"GW{gw} {txt}" for gw, txt in found) or "-"


def status_text(p):
    if p["status"] == "a" and not p.get("news"):
        return ""
    chance = p.get("chance_of_playing_next_round")
    chance_txt = f" ({chance}% to play)" if chance is not None else ""
    return (p.get("news") or "flagged") + chance_txt


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    return out


def build_snapshot(team_id, gw_arg):
    boot = get("bootstrap-static/")
    if not boot:
        raise RuntimeError("Could not load the main FPL data.")
    fixtures = get("fixtures/") or []

    players = {p["id"]: p for p in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    events = boot["events"]

    current = next((e for e in events if e["is_current"]), None)
    upcoming = next((e for e in events if e["is_next"]), None)
    squad_gw = gw_arg or (current["id"] if current else (upcoming["id"] if upcoming else 1))
    fixtures_from = upcoming["id"] if upcoming else squad_gw

    now = datetime.now(timezone.utc)
    L = []
    L.append("# FPL Snapshot")
    L.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M')} UTC (data is a snapshot - re-run the script for fresh data)")
    L.append("")

    # ---- Season state ----
    L.append("## Gameweek status")
    if current:
        L.append(f"- Current/last started gameweek: GW{current['id']} ({'finished' if current['finished'] else 'in progress'})")
    if upcoming:
        L.append(f"- Next gameweek: GW{upcoming['id']}, deadline {upcoming['deadline_time']} (UTC)")
    else:
        L.append("- No upcoming gameweek found (season may be over).")
    L.append("")

    # ---- Manager ----
    entry = get(f"entry/{team_id}/")
    L.append(f"## Manager (team ID {team_id})")
    if not entry:
        L.append("- Team not found. Check the team ID.")
    else:
        L.append(f"- Team: {entry['name']} - manager {entry['player_first_name']} {entry['player_last_name']}")
        L.append(f"- Overall points: {entry['summary_overall_points']}, overall rank: {entry['summary_overall_rank']}")
        L.append(f"- Last GW points: {entry['summary_event_points']}, last GW rank: {entry['summary_event_rank']}")
        L.append(f"- Bank: {money(entry['last_deadline_bank'])}, squad value: {money(entry['last_deadline_value'])}")
        L.append(f"- Total transfers made: {entry['last_deadline_total_transfers']}")
        history = get(f"entry/{team_id}/history/") or {}
        chips = history.get("chips", [])
        used = ", ".join(f"{c['name']} (GW{c['event']})" for c in chips) or "none yet"
        L.append(f"- Chips used: {used}")
    L.append("")

    # ---- Squad ----
    L.append(f"## Squad in GW{squad_gw}")
    picks = get(f"entry/{team_id}/event/{squad_gw}/picks/") if entry else None
    if not picks:
        L.append("- Squad not available for this gameweek.")
    else:
        eh = picks["entry_history"]
        L.append(
            f"- GW points: {eh['points']}, transfers: {eh['event_transfers']} "
            f"(cost {eh['event_transfers_cost']}), chip: {picks.get('active_chip') or 'none'}"
        )
        L.append("- Note: FPL's public data shows the squad as locked at the deadline. "
                 "Transfers you've made since then are not visible here.")
        L.append("")
        rows = []
        for pk in sorted(picks["picks"], key=lambda x: x["position"]):
            p = players[pk["element"]]
            role = "C" if pk["is_captain"] else ("VC" if pk["is_vice_captain"] else "")
            slot = "Bench" if pk["position"] > 11 else "XI"
            rows.append([
                slot, POSITIONS[p["element_type"]], p["web_name"] + (f" ({role})" if role else ""),
                teams[p["team"]]["short_name"], money(p["now_cost"]), p["form"], p["total_points"],
                p["expected_goal_involvements"], fixtures_text(fixtures, teams, p["team"], fixtures_from),
                status_text(p),
            ])
        L += table(["Slot", "Pos", "Player", "Team", "Price", "Form", "Total pts", "xGI", f"Next {LOOKAHEAD} fixtures", "Status"], rows)
    L.append("")

    # ---- Market ----
    def player_rows(plist):
        return [[
            p["web_name"], POSITIONS[p["element_type"]], teams[p["team"]]["short_name"],
            money(p["now_cost"]), p["form"], p["total_points"], p["ep_next"],
            f"{p['selected_by_percent']}%", status_text(p),
        ] for p in plist]

    cols = ["Player", "Pos", "Team", "Price", "Form", "Total pts", "Exp pts next GW", "Owned by", "Status"]
    active = [p for p in players.values() if p["minutes"] > 0]

    L.append("## Top 15 by form")
    L += table(cols, player_rows(sorted(active, key=lambda p: float(p["form"]), reverse=True)[:15]))
    L.append("")
    L.append("## Top 10 by expected points next GW")
    L += table(cols, player_rows(sorted(active, key=lambda p: float(p["ep_next"]), reverse=True)[:10]))
    L.append("")
    L.append("## Top 10 most transferred in this GW")
    L += table(cols, player_rows(sorted(players.values(), key=lambda p: p["transfers_in_event"], reverse=True)[:10]))
    L.append("")
    L.append("## Price changes this GW")
    changed = [p for p in players.values() if p["cost_change_event"] != 0]
    changed.sort(key=lambda p: p["cost_change_event"], reverse=True)
    if changed:
        L += table(["Player", "Team", "Price", "Change"],
                   [[p["web_name"], teams[p["team"]]["short_name"], money(p["now_cost"]),
                     f"{p['cost_change_event'] / 10:+.1f}"] for p in changed])
    else:
        L.append("- None")
    L.append("")

    # ---- Fixtures ----
    L.append(f"## Fixture difficulty, next {LOOKAHEAD} gameweeks (1 = easy, 5 = hard)")
    rows = [[t["name"], fixtures_text(fixtures, teams, t["id"], fixtures_from)]
            for t in sorted(teams.values(), key=lambda t: t["name"])]
    L += table(["Team", "Fixtures"], rows)
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Save live FPL data to a file.")
    ap.add_argument("team_id", nargs="?", type=int, default=DEFAULT_TEAM_ID, help="your FPL team ID")
    ap.add_argument("--gw", type=int, default=None, help="show your squad for this gameweek")
    ap.add_argument("--out", default="fpl_snapshot.md", help="output file name")
    args = ap.parse_args()

    try:
        text = build_snapshot(args.team_id, args.gw)
    except RuntimeError as e:
        print(f"Problem: {e}")
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Done! Saved to {args.out}")


if __name__ == "__main__":
    main()
