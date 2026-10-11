"""Fantasy start/sit emailer for an ESPN league.

Reads my roster, ESPN's weekly projections (already scored with the league's
rules) and the league's lineup slots, builds the highest-projected lineup, and
optionally has Claude review the close calls. Emails the moves to make.

Thursday run: act on players in the Thursday game; the rest is a preview.
Saturday run: full preview after Friday's injury reports.
Sunday run:   final lineup for everyone whose game hasn't started.
"""

import json
import os
import urllib.request
from datetime import datetime

from pydantic import BaseModel

from app import (CENTRAL, RECIPIENT, REGULAR_SEASON, SENDER, UA, MODEL_ID, claude_tool_call,
                 fetch, injuries_by_team, log, parse_games, recent_news, ses, ssm)

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
LEAGUE_ID = os.environ["LEAGUE_ID"]
TEAM_ID = os.environ.get("TEAM_ID", "")
USE_AI = os.environ.get("LINEUP_USE_AI", "true").lower() == "true"
S2_PARAM = os.environ.get("ESPN_S2_PARAM", "/nfl-pickem/espn-s2")
SWID_PARAM = os.environ.get("ESPN_SWID_PARAM", "/nfl-pickem/espn-swid")
CLOSE_CALL = 3.0  # projected points

POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOTS = {0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
         16: "D/ST", 17: "K", 23: "FLEX"}
BENCH, IR = 20, 21
# Statuses that mean the player won't (or very likely won't) play.
SIT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL"}


# ---------------------------------------------------------------- ESPN league

def secret(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def fetch_league(season, week):
    swid = secret(SWID_PARAM)
    url = (f"{FANTASY}/{season}/segments/0/leagues/{LEAGUE_ID}"
           f"?view=mTeam&view=mRoster&view=mSettings&scoringPeriodId={week}")
    headers = {**UA, "Cookie": f"espn_s2={secret(S2_PARAM)}; SWID={swid}"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.load(resp), swid


def my_team(league, swid):
    for team in league.get("teams", []):
        if str(team["id"]) == TEAM_ID or (not TEAM_ID and swid in team.get("owners", [])):
            return team
    raise RuntimeError("couldn't find my team in the league (check SWID or set TeamId)")


def team_name(team):
    return team.get("name") or f"{team.get('location', '')} {team.get('nickname', '')}".strip()


def lineup_slots(league):
    """Starting slot labels in display order, e.g. ['QB', 'RB1', 'RB2', ..., 'FLEX', 'D/ST', 'K']."""
    counts = league["settings"]["rosterSettings"]["lineupSlotCounts"]
    labels = []
    for slot_id, name in SLOTS.items():
        n = int(counts.get(str(slot_id), 0))
        labels += [(f"{name}{i + 1}" if n > 1 else name, slot_id) for i in range(n)]
    return labels


def players(team, games, week):
    by_team = {}
    for g in games:
        by_team[g["home_id"]] = (g, g["away"], "vs")
        by_team[g["away_id"]] = (g, g["home"], "@")

    out = []
    for entry in team["roster"]["entries"]:
        p = entry["playerPoolEntry"]["player"]
        proj = next((s.get("appliedTotal", 0.0) for s in p.get("stats", [])
                     if s.get("scoringPeriodId") == week and s.get("statSourceId") == 1
                     and s.get("statSplitTypeId") == 1), None)
        game, opp, where = by_team.get(p.get("proTeamId"), (None, None, None))
        status = p.get("injuryStatus") or "ACTIVE"
        if entry["lineupSlotId"] == IR:
            status = "INJURY_RESERVE"
        out.append({
            "id": entry["playerId"],
            "name": p["fullName"],
            "pos": POSITIONS.get(p.get("defaultPositionId"), "?"),
            "eligible": set(p.get("eligibleSlots", [])),
            "slot": entry["lineupSlotId"],
            "status": status,
            "proj": proj or 0.0,
            "game": game,
            "team": (game["home"] if game and game["home_id"] == p.get("proTeamId") else
                     game["away"]) if game else "",
            "opp": f"{where} {opp}" if game else "BYE",
            "locked": bool(game and game["started"]),
        })
    return out


def playable(p):
    return p["game"] is not None and p["status"] not in SIT_STATUSES


# ---------------------------------------------------------------- lineup

def best_lineup(roster, slots):
    """Assign the highest projections to starting slots.

    Players whose game started stay put. Remaining slots are filled most
    restrictive first (fewest eligible players), each with the best projection
    left, which is optimal for standard QB/RB/WR/TE/FLEX/D/ST/K setups.
    """
    lineup = {}
    used = set()
    open_slots = []
    current = {}
    for p in roster:
        current.setdefault(p["slot"], []).append(p)
    taken = {}
    for label, slot_id in slots:
        locked_here = [p for p in current.get(slot_id, []) if p["locked"] and p["id"] not in used]
        if locked_here:
            lineup[label] = locked_here[0]
            used.add(locked_here[0]["id"])
        else:
            open_slots.append((label, slot_id))
    # Locked bench/IR players can't move into the lineup either.
    pool = [p for p in roster if p["id"] not in used and not p["locked"] and playable(p)]

    def eligible(slot_id):
        return [p for p in pool if slot_id in p["eligible"] and p["id"] not in taken]

    for label, slot_id in sorted(open_slots, key=lambda s: len(eligible(s[1]))):
        options = sorted(eligible(slot_id), key=lambda p: -p["proj"])
        if options:
            lineup[label] = options[0]
            taken[options[0]["id"]] = label
    return {label: lineup.get(label) for label, _ in slots}


def close_calls(roster, lineup, slots):
    """Starters with an injury tag, or a bench player within CLOSE_CALL of a starter they could replace."""
    slot_of = dict(slots)
    starters = {p["id"] for p in lineup.values() if p}
    calls = []
    for label, p in lineup.items():
        if not p or p["locked"]:
            continue
        if p["status"] not in ("ACTIVE", "NORMAL"):
            calls.append(f"{p['name']} ({label}) is {p['status'].lower()}")
        for b in roster:
            if (b["id"] not in starters and not b["locked"] and playable(b)
                    and slot_of[label] in b["eligible"] and p["proj"] - b["proj"] <= CLOSE_CALL):
                calls.append(f"{label}: {p['name']} {p['proj']:.1f} vs {b['name']} {b['proj']:.1f}")
    return calls


class SlotPick(BaseModel):
    slot: str
    player_id: int


class Reason(BaseModel):
    player_id: int
    reason: str


class LineupSheet(BaseModel):
    lineup: list[SlotPick]
    reasons: list[Reason]
    summary: str


def describe(p):
    g = p["game"]
    game = (f"{p['opp']}, {g['kickoff_ct']:%a %I:%M %p} CT, line {g['line']}, O/U {g['over_under']}"
            if g else "BYE")
    lock = ", GAME STARTED (can't move)" if p["locked"] else ""
    return (f"- id={p['id']} {p['name']} {p['pos']} {p['team']} | proj {p['proj']:.1f} | "
            f"{p['status'].lower()} | {game} | eligible: "
            f"{', '.join(SLOTS[s] for s in sorted(p['eligible']) if s in SLOTS)}{lock}")


def ai_lineup(roster, slots, proposed, calls):
    teams = {p["team"] for p in roster if p["team"]}
    injuries = injuries_by_team(teams)
    lines = [
        "You're setting my fantasy football lineup for this week (ESPN league, PPR scoring).",
        "Below is my roster with ESPN's projected points, and a lineup built purely from those "
        "projections. Review the close calls listed, using injury designations, teammates' injuries "
        "(e.g. a backup QB starting), game script (line and over/under) and news. Keep the "
        "projection lineup unless you have a concrete reason to change it.",
        "Rules: fill every slot listed below with an eligible player, use each player once, never "
        "start someone who is out or on bye, and keep players whose game started where they are.",
        "Give a short reason for every starter in a close call and every change you make.",
        "\nSLOTS: " + ", ".join(label for label, _ in slots),
        "\nROSTER:", *[describe(p) for p in roster],
        "\nPROJECTION LINEUP:",
        *[f"- {label}: {p['name']} (id={p['id']})" if p else f"- {label}: (empty)"
          for label, p in proposed.items()],
        "\nCLOSE CALLS:", *[f"- {c}" for c in calls],
        "\nINJURY REPORTS FOR MY PLAYERS' TEAMS:",
        *[f"{t}: " + "; ".join(items) for t, items in injuries.items() if items],
        "\nRECENT NFL NEWS:", *recent_news(),
    ]
    sheet = LineupSheet.model_validate(claude_tool_call(
        "\n".join(lines), "submit_lineup",
        "Submit the final lineup (slot label -> player id), reasons, and a short summary.",
        LineupSheet.model_json_schema()))
    return validate(sheet, roster, slots, proposed), {r.player_id: r.reason for r in sheet.reasons}, sheet.summary


def validate(sheet, roster, slots, proposed):
    by_id = {p["id"]: p for p in roster}
    slot_of = dict(slots)
    picks = {s.slot: s.player_id for s in sheet.lineup}
    if set(picks) != set(slot_of) or len(set(picks.values())) != len(picks):
        raise ValueError("lineup doesn't fill each slot with a different player")
    lineup = {}
    for label, pid in picks.items():
        p = by_id.get(pid)
        if p is None or slot_of[label] not in p["eligible"]:
            raise ValueError(f"player {pid} can't play {label}")
        if not playable(p) and not p["locked"]:
            raise ValueError(f"{p['name']} is {p['status'].lower() if p['game'] else 'on bye'}")
        lineup[label] = p
    for label, p in proposed.items():
        if p and p["locked"] and lineup[label]["id"] != p["id"]:
            raise ValueError(f"{p['name']}'s game already started")
    if any(p["locked"] and p["slot"] in (BENCH, IR) for p in lineup.values()):
        raise ValueError("started a benched player whose game already started")
    return {label: lineup[label] for label, _ in slots}


# ---------------------------------------------------------------- email

def moves(roster, lineup, run):
    """Start/bench changes vs. my current ESPN lineup."""
    starting = {p["id"]: label for label, p in lineup.items() if p}
    out = []
    for p in roster:
        was = p["slot"] not in (BENCH, IR)
        now = p["id"] in starting
        if was == now or p["locked"]:
            continue
        thursday = p["game"] and p["game"]["kickoff_ct"].weekday() == 3
        when = " (plays Thursday, do this now)" if thursday and run == "thursday" else ""
        out.append(f"Start {p['name']} at {starting[p['id']]}{when}" if now else f"Bench {p['name']}{when}")
    return sorted(out, key=lambda m: ("Thursday" not in m, not m.startswith("Start")))


def why(p, reasons):
    if p["id"] in reasons:
        return reasons[p["id"]]
    if not p["game"]:
        return "Bye week"
    if p["status"] in SIT_STATUSES:
        return p["status"].replace("_", " ").title()
    return ""


def row(label, p, reasons, html):
    if not p:
        cells = [label, "(empty)", "", "", "", "", "", ""]
    else:
        g = p["game"]
        cells = [label, p["name"], p["team"], p["opp"],
                 f"{g['kickoff_ct']:%a %I:%M %p}" if g else "", f"{p['proj']:.1f}",
                 p["status"].replace("_", " ").title(), why(p, reasons)]
    if html:
        return "<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in cells) + "</tr>"
    return f"{cells[0]:<6} {cells[1]:<24} {cells[3]:<8} {cells[5]:>5}  {cells[6]:<12} {cells[7]}"


def send_email(subject, team, roster, lineup, actions, notes, summary, reasons):
    starting = {p["id"] for p in lineup.values() if p}
    bench = sorted((p for p in roster if p["id"] not in starting), key=lambda p: -p["proj"])
    total = sum(p["proj"] for p in lineup.values() if p)
    header = ["Slot", "Player", "Team", "Opp", "Kickoff (CT)", "Proj", "Status", "Why"]

    def table(rows):
        return ("<table style='border-collapse:collapse' border='1'><tr>"
                + "".join(f"<th>{h}</th>" for h in header) + "</tr>" + "".join(rows) + "</table>")

    actions_html = ("<h3>Action needed</h3><ul>" + "".join(f"<li><b>{a}</b></li>" for a in actions) + "</ul>"
                    if actions else "<h3>No changes needed</h3><p>Your current ESPN lineup is the one to play.</p>")
    html = (
        f"<html><body style='font-family:sans-serif'><p>{team_name(team)}</p>{actions_html}"
        + (f"<p>{summary}</p>" if summary else "")
        + f"<h3>Lineup (projected {total:.1f})</h3>"
        + table(row(label, p, reasons, True) for label, p in lineup.items())
        + "<h3>Bench</h3>" + table(row("BE", p, reasons, True) for p in bench)
        + "".join(f"<p style='color:#555'>{n}</p>" for n in notes) + "</body></html>"
    )
    text = "\n".join(
        [team_name(team), ""] + (["ACTION NEEDED:"] + [f"  - {a}" for a in actions] if actions
                                 else ["No changes needed."])
        + ([""] + [summary] if summary else [])
        + ["", f"LINEUP (projected {total:.1f}):"] + [row(label, p, reasons, False) for label, p in lineup.items()]
        + ["", "BENCH:"] + [row("BE", p, reasons, False) for p in bench] + [""] + notes
    )
    ses.send_email(
        FromEmailAddress=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Content={"Simple": {
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html}, "Text": {"Data": text}},
        }},
    )


# ---------------------------------------------------------------- handler

def handler(event, context):
    event = event or {}
    now_ct = datetime.now(CENTRAL)
    run = event.get("run") or {3: "thursday", 5: "saturday"}.get(now_ct.weekday(), "sunday")
    test = bool(event.get("test"))

    sb = fetch("scoreboard")
    if sb.get("season", {}).get("type") != REGULAR_SEASON:
        log.info("not the regular season (%s); no email", sb.get("season"))
        return {"status": "off season"}
    season = sb.get("season", {}).get("year", now_ct.year)
    week = sb.get("week", {}).get("number")
    games = parse_games(sb)

    league, swid = fetch_league(season, week)
    team = my_team(league, swid)
    slots = lineup_slots(league)
    roster = players(team, games, week)
    lineup = best_lineup(roster, slots)
    notes, reasons, summary = [], {}, ""

    calls = close_calls(roster, lineup, slots)
    if not USE_AI:
        notes.append("Lineup from ESPN projections (AI off).")
    elif not calls:
        notes.append("Lineup from ESPN projections (no close calls for Claude to review).")
    else:
        try:
            lineup, reasons, summary = ai_lineup(roster, slots, lineup, calls)
            notes.append(f"Close calls reviewed by {MODEL_ID} on Amazon Bedrock.")
        except Exception as exc:  # any AI failure still gets an email
            log.exception("AI lineup failed")
            notes.append(f"AI review failed ({type(exc).__name__}: {exc}); lineup from ESPN projections.")

    if run == "thursday":
        notes.append("Only Thursday-game players need a decision today; the rest is a preview.")
    if any(not p for p in lineup.values()):
        notes.append("Some slots have no healthy eligible player; check waivers/free agents.")
    actions = moves(roster, lineup, run)

    label = {"thursday": "Thursday", "saturday": "Saturday preview", "sunday": "Sunday final"}[run]
    subject = f"{'[TEST] ' if test else ''}Fantasy Week {week} Lineup - {label}"
    send_email(subject, team, roster, lineup, actions, notes, summary, reasons)
    log.info("sent lineup for week %s (%s): %d moves", week, run, len(actions))
    return {"status": "sent", "week": week, "run": run, "moves": actions,
            "lineup": {k: (p["name"] if p else None) for k, p in lineup.items()}}
