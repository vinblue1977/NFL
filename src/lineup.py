"""Fantasy start/sit emailer for an ESPN league.

Reads my roster, ESPN's weekly projections (already scored with the league's
rules) and the league's lineup slots, builds the highest-projected lineup, and
optionally has Claude review the close calls. Emails the moves to make.

Thursday run: act on players in the Thursday game; the rest is a preview.
Saturday run: full preview after Friday's injury reports.
Sunday run:   final lineup for everyone whose game hasn't started.

Alert runs (early and late Sunday, Monday evening) re-check the lineup once a
game's inactives are out. They do nothing unless a game kicks off within two
hours, and email only if a start/bench change is needed that the last alert
didn't already tell you about.
Waiver run (Tuesday): emails only the free-agent pickups, and only if there are any.
Recap run (Tuesday): once the week is over, emails how the lineup scored against ESPN's projections,
what the best lineup in hindsight would have scored, and the matchup result.

Every other run also suggests free-agent pickups: the best available players by
ESPN's rest-of-season projection, checked against recent player news.
"""

import hashlib
import html
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import cache

from pydantic import BaseModel

from app import (
    ALERT_PREFIX,
    CENTRAL,
    MODEL_ID,
    REGULAR_SEASON,
    UA,
    claude_tool_call,
    deliver,
    fetch,
    final_winners,
    heartbeat,
    injuries_by_team,
    load_param,
    log,
    notify,
    parse_games,
    recent_news,
    save_param,
    ssm,
)

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
PLAYER_NEWS = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
LEAGUE_ID = os.environ["LEAGUE_ID"]
TEAM_ID = os.environ.get("TEAM_ID", "")
USE_AI = os.environ.get("LINEUP_USE_AI", "true").lower() == "true"
S2_PARAM = os.environ.get("ESPN_S2_PARAM", "/nfl-pickem/espn-s2")
SWID_PARAM = os.environ.get("ESPN_SWID_PARAM", "/nfl-pickem/espn-swid")
CLOSE_CALL = 3.0  # projected points
ALERT_WINDOW = 2 * 3600  # alert runs only matter just before a kickoff (seconds)
COOKIE_ALERT_PARAM = os.environ.get("COOKIE_ALERT_PARAM", "/nfl-pickem/cookie-alert")
COOKIE_ALERT_GAP = timedelta(hours=20)  # one warning a day, not one per run
FUNCTION = "nfl-fantasy-lineup"
RECAP_PARAM = os.environ.get("RECAP_PARAM", "/nfl-pickem/recap")
RUNS = {"thursday", "saturday", "sunday", "alert", "waivers", "recap"}

POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOTS = {0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP", 16: "D/ST", 17: "K", 23: "FLEX"}
BENCH, IR = 20, 21
# Statuses that mean the player won't (or very likely won't) play.
SIT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL"}

# Free-agent pickups
CANDIDATES = {"QB": 4, "RB": 6, "WR": 6, "TE": 4, "K": 3, "D/ST": 3}  # best available per position
MIN_GAIN = 15.0  # rest-of-season points a projection-only swap must gain
MAX_PICKUPS = 3
NEWS_DAYS = 14
HEALTHY = {"ACTIVE", "NORMAL", "QUESTIONABLE", "DAY_TO_DAY"}


# ---------------------------------------------------------------- ESPN league


class EspnCookieError(Exception):
    """The espn_s2/SWID cookies were rejected, so the private league can't be read."""


@cache
def secret(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def fantasy_get(path, player_filter=None):
    headers = {**UA, "Cookie": f"espn_s2={secret(S2_PARAM)}; SWID={secret(SWID_PARAM)}"}
    if player_filter:
        headers["X-Fantasy-Filter"] = json.dumps(player_filter)
    req = urllib.request.Request(f"{FANTASY}/{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):  # ESPN's answer to a missing, wrong or expired espn_s2/SWID
            raise EspnCookieError(f"ESPN refused the league request (HTTP {exc.code})") from exc
        raise


def fetch_league(season, week, matchups=False):
    views = "view=mTeam&view=mRoster&view=mSettings" + ("&view=mMatchupScore" if matchups else "")
    league = fantasy_get(f"{season}/segments/0/leagues/{LEAGUE_ID}?{views}&scoringPeriodId={week}")
    return league, secret(SWID_PARAM)


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
        proj = next(
            (
                s.get("appliedTotal", 0.0)
                for s in p.get("stats", [])
                if s.get("scoringPeriodId") == week and s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
            ),
            None,
        )
        actual = next(  # what the player really scored that week (source 0 = actual, split 1 = one scoring period)
            (
                s.get("appliedTotal", 0.0)
                for s in p.get("stats", [])
                if s.get("scoringPeriodId") == week and s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 1
            ),
            None,
        )
        game, opp, where = by_team.get(p.get("proTeamId"), (None, None, None))
        status = p.get("injuryStatus") or "ACTIVE"
        if entry["lineupSlotId"] == IR:
            status = "INJURY_RESERVE"
        out.append(
            {
                "id": entry["playerId"],
                "name": p["fullName"],
                "pos": POSITIONS.get(p.get("defaultPositionId"), "?"),
                "eligible": set(p.get("eligibleSlots", [])),
                "slot": entry["lineupSlotId"],
                "status": status,
                "proj": proj or 0.0,
                "actual": actual or 0.0,
                "game": game,
                "team": (game["home"] if game and game["home_id"] == p.get("proTeamId") else game["away"]) if game else "",
                "opp": f"{where} {opp}" if game else "BYE",
                "locked": bool(game and game["started"]),
            }
        )
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
            if (
                b["id"] not in starters
                and not b["locked"]
                and playable(b)
                and slot_of[label] in b["eligible"]
                and p["proj"] - b["proj"] <= CLOSE_CALL
            ):
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
    game = f"{p['opp']}, {g['kickoff_ct']:%a %I:%M %p} CT, line {g['line']}, O/U {g['over_under']}" if g else "BYE"
    lock = ", GAME STARTED (can't move)" if p["locked"] else ""
    return (
        f"- id={p['id']} {p['name']} {p['pos']} {p['team']} | proj {p['proj']:.1f} | "
        f"{p['status'].lower()} | {game} | eligible: "
        f"{', '.join(SLOTS[s] for s in sorted(p['eligible']) if s in SLOTS)}{lock}"
    )


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
        "\nROSTER:",
        *[describe(p) for p in roster],
        "\nPROJECTION LINEUP:",
        *[f"- {label}: {p['name']} (id={p['id']})" if p else f"- {label}: (empty)" for label, p in proposed.items()],
        "\nCLOSE CALLS:",
        *[f"- {c}" for c in calls],
        "\nINJURY REPORTS FOR MY PLAYERS' TEAMS:",
        *[f"{t}: " + "; ".join(items) for t, items in injuries.items() if items],
        "\nRECENT NFL NEWS:",
        *recent_news(),
    ]
    sheet = LineupSheet.model_validate(
        claude_tool_call(
            "\n".join(lines),
            "submit_lineup",
            "Submit the final lineup (slot label -> player id), reasons, and a short summary.",
            LineupSheet.model_json_schema(),
        )
    )
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


# ---------------------------------------------------------------- pickups


def fetch_free_agents(season, week):
    player_filter = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": [0, 2, 4, 6, 16, 17]},  # QB, RB, WR, TE, D/ST, K
            "limit": 300,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    return fantasy_get(f"{season}/segments/0/leagues/{LEAGUE_ID}?view=kona_player_info&scoringPeriodId={week}", player_filter)[
        "players"
    ]


def pro_teams(season):
    """ESPN pro team id -> (abbreviation, bye week)."""
    data = fantasy_get(f"{season}?view=proTeamSchedules_wl")
    return {t["id"]: (t["abbrev"], t.get("byeWeek")) for t in data["settings"]["proTeams"]}


def season_stat(p, season, source, split, period=0):
    """source 0 = actual, 1 = projection; split 0 = season (rest of season for projections), 1 = week."""
    return next(
        (
            s
            for s in p.get("stats", [])
            if s.get("seasonId") == season
            and s.get("statSourceId") == source
            and s.get("statSplitTypeId") == split
            and s.get("scoringPeriodId") == period
        ),
        {},
    )


def outlook(p, season, week, teams, started, availability=""):
    actual, ros = season_stat(p, season, 0, 0), season_stat(p, season, 1, 0)
    abbr, bye = teams.get(p.get("proTeamId"), ("?", None))
    return {
        "id": p["id"],
        "name": p["fullName"],
        "pos": POSITIONS.get(p.get("defaultPositionId"), "?"),
        "team": abbr,
        "bye": bye,
        "status": p.get("injuryStatus") or "ACTIVE",
        "week": season_stat(p, season, 1, 1, week).get("appliedTotal") or 0.0,
        "avg": actual.get("appliedAverage") or 0.0,
        "ros": ros.get("appliedTotal") or 0.0,
        "ros_avg": ros.get("appliedAverage") or 0.0,
        "owned": p.get("ownership", {}).get("percentOwned", 0.0),
        "locked": p.get("proTeamId") in started,
        "availability": availability,  # FREEAGENT or WAIVERS for players I don't have
        "news": [],
    }


def player_news(player_id):
    """Recent Rotowire blurbs for a player (ESPN fantasy player news)."""
    cutoff = datetime.now(UTC) - timedelta(days=NEWS_DAYS)
    req = urllib.request.Request(f"{PLAYER_NEWS}?limit=10&playerId={player_id}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            feed = json.load(resp).get("feed", [])
    except Exception:
        log.warning("news fetch failed for player %s", player_id, exc_info=True)
        return []
    out = []
    for item in feed:
        try:
            when = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
        except KeyError, ValueError:
            continue
        if item.get("type") == "Rotowire" and when >= cutoff:
            out.append(
                {
                    "when": f"{when:%m/%d}",
                    "headline": item.get("headline", ""),
                    "story": " ".join((item.get("story") or "").split())[:400],
                }
            )
        if len(out) == 3:
            break
    return out


def projection_pickups(pool, droppable):
    """Same-position swaps that gain at least MIN_GAIN projected rest-of-season points."""
    moves, dropped = [], set()
    for fa in sorted((p for p in pool if p["status"] in HEALTHY), key=lambda p: -p["ros"]):
        same = [d for d in droppable if d["pos"] == fa["pos"] and d["id"] not in dropped]
        if not same:
            continue
        worst = min(same, key=lambda d: d["ros"])
        if fa["ros"] - worst["ros"] >= MIN_GAIN:
            latest = f" Latest ({fa['news'][0]['when']}): {fa['news'][0]['headline']}" if fa["news"] else ""
            moves.append((fa, worst, f"+{fa['ros'] - worst['ros']:.0f} projected rest-of-season points.{latest}"))
            dropped.add(worst["id"])
        if len(moves) == MAX_PICKUPS:
            break
    return moves


class Pickup(BaseModel):
    add_player_id: int
    add_player_name: str
    drop_player_id: int
    drop_player_name: str
    reason: str


class PickupSheet(BaseModel):
    pickups: list[Pickup]
    summary: str


def describe_outlook(p):
    where = {"FREEAGENT": "free agent", "WAIVERS": "on waivers (needs a claim)"}.get(p["availability"], "")
    line = (
        f"- id={p['id']} {p['name']} {p['pos']} {p['team']} (bye wk {p['bye']}) | "
        f"{p['status'].lower()} | this week {p['week']:.1f} | season avg {p['avg']:.1f} | "
        f"rest of season {p['ros']:.0f} ({p['ros_avg']:.1f}/g) | {p['owned']:.0f}% rostered in ESPN leagues"
        + (f" | {where}" if where else "")
    )
    return "\n".join([line] + [f"    news {n['when']} {n['headline']} {n['story']}" for n in p["news"]])


def ai_pickups(pool, mine, droppable, slots, week):
    lines = [
        f"You're managing my ESPN fantasy football team (PPR) in week {week}. Suggest free-agent "
        f"pickups: at most {MAX_PICKUPS} add/drop moves, each adding one AVAILABLE player and "
        "dropping one of MY droppable players.",
        "Use ESPN's projections as the baseline and the player news to check them: role changes, "
        "injuries to the player or to teammates ahead of them, depth chart and snap share, "
        "suspensions, and anything the projection may not reflect yet. Weigh rest-of-season value "
        "over one good week, but cover my bye-week and injury holes for this week too.",
        "Only suggest a move that clearly improves my team; an empty list is a fine answer. Don't "
        "drop a player just because they're on bye or had one bad week. Mention when a player is on "
        "waivers (needs a claim) rather than a free agent.",
        "\nLINEUP SLOTS: " + ", ".join(label for label, _ in slots),
        "\nMY ROSTER:",
        *[describe_outlook(p) for p in mine],
        "\nDROPPABLE: " + ", ".join(f"{p['name']} (id={p['id']})" for p in droppable),
        "\nBEST AVAILABLE PLAYERS:",
        *[describe_outlook(p) for p in pool],
    ]
    sheet = PickupSheet.model_validate(
        claude_tool_call(
            "\n".join(lines),
            "submit_pickups",
            "Submit the pickups (add and drop player ids with their exact names as listed, a short "
            "reason citing the news) and a one-paragraph summary.",
            PickupSheet.model_json_schema(),
        )
    )
    adds, drops = {p["id"]: p for p in pool}, {p["id"]: p for p in droppable}
    picks = sheet.pickups[:MAX_PICKUPS]
    for m in picks:
        if m.add_player_id not in adds:
            raise ValueError(f"player {m.add_player_id} isn't an available candidate")
        if m.drop_player_id not in drops:
            raise ValueError(f"player {m.drop_player_id} isn't on my droppable list")
        # Catches an id that doesn't match the player the reason talks about.
        if adds[m.add_player_id]["name"] != m.add_player_name or drops[m.drop_player_id]["name"] != m.drop_player_name:
            raise ValueError(f"ids don't match names ({m.add_player_name} / {m.drop_player_name})")
    if len({m.add_player_id for m in picks}) != len(picks) or len({m.drop_player_id for m in picks}) != len(picks):
        raise ValueError("a player is added or dropped twice")
    return [(adds[m.add_player_id], drops[m.drop_player_id], m.reason) for m in picks], sheet.summary


def suggest_pickups(season, week, team, games, slots):
    """Returns ([(add, drop, reason)], summary, note)."""
    teams = pro_teams(season)
    started = {g["home_id"] for g in games if g["started"]} | {g["away_id"] for g in games if g["started"]}
    entries = team["roster"]["entries"]
    mine = [outlook(e["playerPoolEntry"]["player"], season, week, teams, started) for e in entries]
    # IR slots don't count against the roster, and players whose game started can't be dropped.
    droppable = [p for p, e in zip(mine, entries, strict=True) if e["lineupSlotId"] != IR and not p["locked"]]

    available = [outlook(x["player"], season, week, teams, started, x.get("status", "")) for x in fetch_free_agents(season, week)]
    pool = []
    for pos, n in CANDIDATES.items():
        pool += sorted((p for p in available if p["pos"] == pos and not p["locked"]), key=lambda p: -p["ros"])[:n]

    everyone = mine + pool
    with ThreadPoolExecutor(8) as ex:
        for p, news in zip(everyone, ex.map(player_news, [p["id"] for p in everyone]), strict=True):
            p["news"] = news

    if USE_AI:
        try:
            moves, summary = ai_pickups(pool, mine, droppable, slots, week)
            return moves, summary, f"Pickups chosen by {MODEL_ID} from ESPN projections and player news."
        except Exception as exc:  # fall back to projections
            log.exception("AI pickups failed")
            note = f"AI pickup review failed ({type(exc).__name__}: {exc}); pickups from projections."
    else:
        note = "Pickups from ESPN rest-of-season projections (AI off); check the news before adding."
    return projection_pickups(pool, droppable), "", note


# ---------------------------------------------------------------- email


def kicks_soon(game, now):
    """True if the game hasn't started and kicks off within ALERT_WINDOW."""
    return bool(game) and not game["started"] and 0 < (game["kickoff_ct"] - now).total_seconds() <= ALERT_WINDOW


def moves(roster, lineup, run, now=None):
    """Start/bench changes vs. my current ESPN lineup; alert runs only report players kicking off soon."""
    starting = {p["id"]: label for label, p in lineup.items() if p}
    out = []
    for p in roster:
        was = p["slot"] not in (BENCH, IR)
        starts = p["id"] in starting
        if was == starts or p["locked"] or (run == "alert" and not kicks_soon(p["game"], now)):
            continue
        thursday = p["game"] and p["game"]["kickoff_ct"].weekday() == 3
        when = " (plays Thursday, do this now)" if thursday and run == "thursday" else ""
        out.append(f"Start {p['name']} at {starting[p['id']]}{when}" if starts else f"Bench {p['name']}{when}")
    return sorted(out, key=lambda m: ("Thursday" not in m, not m.startswith("Start")))


def why(p, reasons):
    if p["id"] in reasons:
        return reasons[p["id"]]
    if not p["game"]:
        return "Bye week"
    if p["status"] in SIT_STATUSES:
        return p["status"].replace("_", " ").title()
    return ""


def row(label, p, reasons, as_html):
    if not p:
        cells = [label, "(empty)", "", "", "", "", "", ""]
    else:
        g = p["game"]
        cells = [
            label,
            p["name"],
            p["team"],
            p["opp"],
            f"{g['kickoff_ct']:%a %I:%M %p}" if g else "",
            f"{p['proj']:.1f}",
            p["status"].replace("_", " ").title(),
            why(p, reasons),
        ]
    if as_html:
        return "<tr>" + "".join(f"<td style='padding:4px 8px'>{html.escape(str(c))}</td>" for c in cells) + "</tr>"
    return f"{cells[0]:<6} {cells[1]:<24} {cells[3]:<8} {cells[5]:>5}  {cells[6]:<12} {cells[7]}"


def pickup_cells(add, drop, reason):
    waivers = " (waivers)" if add["availability"] == "WAIVERS" else ""
    return [
        f"{add['name']}{waivers}",
        f"{add['pos']} {add['team']}",
        f"{add['ros_avg']:.1f}",
        drop["name"],
        f"{drop['ros_avg']:.1f}",
        reason,
    ]


def pickups_html(pickups, summary):
    if not pickups:
        return "<h3>Free-agent pickups</h3><p>No pickups worth making this week.</p>" + (
            f"<p>{html.escape(summary)}</p>" if summary else ""
        )
    header = ["Add", "Pos", "Proj/g", "Drop", "Proj/g", "Why"]
    rows = "".join(
        "<tr>" + "".join(f"<td style='padding:4px 8px'>{html.escape(str(c))}</td>" for c in pickup_cells(*m)) + "</tr>"
        for m in pickups
    )
    return (
        "<h3>Free-agent pickups</h3>"
        + (f"<p>{html.escape(summary)}</p>" if summary else "")
        + "<table style='border-collapse:collapse' border='1'><tr>"
        + "".join(f"<th>{h}</th>" for h in header)
        + "</tr>"
        + rows
        + "</table>"
        + "<p style='color:#555'>Proj/g = ESPN rest-of-season points per game.</p>"
    )


def pickups_text(pickups, summary):
    if not pickups:
        return ["FREE-AGENT PICKUPS: none worth making this week."] + ([summary] if summary else [])
    lines = ["FREE-AGENT PICKUPS (rest-of-season points per game):"] + ([summary] if summary else [])
    for m in pickups:
        add, pos, add_avg, drop, drop_avg, reason = pickup_cells(*m)
        lines.append(f"  - Add {add} ({pos}, {add_avg}), drop {drop} ({drop_avg}): {reason}")
    return lines


def send_email(subject, team, roster, lineup, actions, notes, summary, reasons, pickups, pickup_summary):
    starting = {p["id"] for p in lineup.values() if p}
    bench = sorted((p for p in roster if p["id"] not in starting), key=lambda p: -p["proj"])
    total = sum(p["proj"] for p in lineup.values() if p)
    header = ["Slot", "Player", "Team", "Opp", "Kickoff (CT)", "Proj", "Status", "Why"]
    pickups_part = pickups_html(pickups, pickup_summary) if pickups is not None else ""
    pickups_lines = ([""] + pickups_text(pickups, pickup_summary)) if pickups is not None else []

    def table(rows):
        return (
            "<table style='border-collapse:collapse' border='1'><tr>"
            + "".join(f"<th>{h}</th>" for h in header)
            + "</tr>"
            + "".join(rows)
            + "</table>"
        )

    actions_html = (
        "<h3>Action needed</h3><ul>" + "".join(f"<li><b>{html.escape(a)}</b></li>" for a in actions) + "</ul>"
        if actions
        else "<h3>No changes needed</h3><p>Your current ESPN lineup is the one to play.</p>"
    )
    body = (
        f"<html><body style='font-family:sans-serif'><p>{html.escape(team_name(team))}</p>{actions_html}"
        + (f"<p>{html.escape(summary)}</p>" if summary else "")
        + f"<h3>Lineup (projected {total:.1f})</h3>"
        + table(row(label, p, reasons, True) for label, p in lineup.items())
        + "<h3>Bench</h3>"
        + table(row("BE", p, reasons, True) for p in bench)
        + pickups_part
        + "".join(f"<p style='color:#555'>{html.escape(n)}</p>" for n in notes)
        + "</body></html>"
    )
    text = "\n".join(
        [team_name(team), ""]
        + (["ACTION NEEDED:"] + [f"  - {a}" for a in actions] if actions else ["No changes needed."])
        + ([""] + [summary] if summary else [])
        + ["", f"LINEUP (projected {total:.1f}):"]
        + [row(label, p, reasons, False) for label, p in lineup.items()]
        + ["", "BENCH:"]
        + [row("BE", p, reasons, False) for p in bench]
        + pickups_lines
        + [""]
        + notes
    )
    deliver(subject, body, text)


# ---------------------------------------------------------------- handler


def alert_name(season, week):
    return f"{ALERT_PREFIX}/{season}-w{week}"


def digest(action):
    return hashlib.sha256(action.encode()).hexdigest()[:12]


def sent_alerts(season, week):
    """Digests of the moves already emailed this week. SSM trouble reads as 'none', so an alert is never lost."""
    try:
        return set(json.loads(ssm.get_parameter(Name=alert_name(season, week))["Parameter"]["Value"]))
    except ssm.exceptions.ParameterNotFound:
        return set()
    except Exception:
        log.exception("couldn't read the saved alerts; treating every move as new")
        return set()


def save_alerts(season, week, digests):
    try:
        ssm.put_parameter(Name=alert_name(season, week), Value=json.dumps(sorted(digests)), Type="String", Overwrite=True)
    except Exception:  # the email already went out; a missed save only risks one repeat
        log.exception("couldn't save the sent alerts")


def alert_is_new(season, week, actions):
    """True if any of these moves wasn't in an earlier alert this week."""
    sent = sent_alerts(season, week)
    return any(digest(a) not in sent for a in actions)


def remember_alert(season, week, actions):
    save_alerts(season, week, sent_alerts(season, week) | {digest(a) for a in actions})


def forget_if_settled(season, week, roster, lineup):
    """Clear the saved alerts once no change is needed at all (not just none soon), so a returning problem alerts again."""
    if not moves(roster, lineup, "sunday") and sent_alerts(season, week):
        save_alerts(season, week, set())


def waiver_run(season, week, team, slots, test):
    """Tuesday: email just the pickups, ahead of the Wednesday waiver run. Silent if there are none."""
    pickups, summary, note = suggest_pickups(season, week, team, [], slots)
    if not pickups and not test:
        log.info("week %s waivers: nothing worth claiming, no email", week)
        return {"status": "no pickups", "week": week, "run": "waivers"}
    body = (
        f"<html><body style='font-family:sans-serif'><p>{html.escape(team_name(team))}</p>"
        + pickups_html(pickups, summary)
        + f"<p style='color:#555'>{html.escape(note)}</p></body></html>"
    )
    text = "\n".join([team_name(team), ""] + pickups_text(pickups, summary) + ["", note])
    deliver(f"{'[TEST] ' if test else ''}Fantasy Week {week} Waiver Pickups", body, text)
    log.info("sent waiver pickups for week %s: %d", week, len(pickups))
    return {
        "status": "sent",
        "week": week,
        "run": "waivers",
        "pickups": [f"add {a['name']}, drop {d['name']}" for a, d, _ in pickups],
    }


# ---------------------------------------------------------------- recap


def hindsight(roster, slots):
    """The best lineup the roster could have fielded, judged by what everyone actually scored."""
    judged = [  # anyone who scored played, even if the scoreboard lost track of his game
        {**p, "proj": p["actual"], "locked": False, "status": "ACTIVE", "game": p["game"] or ({} if p["actual"] else None)}
        for p in roster
        if p["slot"] != IR
    ]
    return best_lineup(judged, slots)


def matchup_periods(league, week):
    """Matchup period ids that cover this NFL week (they are the same number unless the league doubles up weeks)."""
    periods = (league.get("settings", {}).get("scheduleSettings", {}).get("matchupPeriods")) or {}
    ids = {int(k) for k, weeks in periods.items() if week in weeks}
    return ids or {week}


def matchup_result(league, team, week):
    """(my points, opponent name, opponent points) for that week's matchup, or None (bye, eliminated, not scored)."""
    names = {t["id"]: team_name(t) for t in league.get("teams", [])}
    periods = matchup_periods(league, week)
    for m in league.get("schedule", []):
        home, away = m.get("home") or {}, m.get("away") or {}
        if m.get("matchupPeriodId") not in periods or team["id"] not in (home.get("teamId"), away.get("teamId")):
            continue
        me, opp = (home, away) if home.get("teamId") == team["id"] else (away, home)
        if "totalPoints" in me and "totalPoints" in opp and (me["totalPoints"] or opp["totalPoints"]):  # 0-0: not tallied yet
            return me["totalPoints"], names.get(opp.get("teamId"), "your opponent"), opp["totalPoints"]
    return None


def recap_week(sb):
    """The week the recap is about: ESPN's current week, or the one before it once ESPN has rolled over to the next."""
    week = sb.get("week", {}).get("number")
    if week is None:
        return None
    games = parse_games(sb)
    return week - 1 if games and not any(g["started"] for g in games) else week


def recap_facts(roster, slots, result):
    starters = [p for p in roster if p["slot"] not in (BENCH, IR)]
    order = {slot_id: i for i, (_, slot_id) in reversed(list(enumerate(slots)))}
    starters.sort(key=lambda p: (order.get(p["slot"], 99), -p["actual"]))
    best = {p["id"]: p for p in hindsight(roster, slots).values() if p}
    started = {p["id"] for p in starters}
    scored = sum(p["actual"] for p in starters)
    projected = sum(p["proj"] for p in starters)
    return {
        "starters": starters,
        "scored": scored,
        "projected": projected or None,  # ESPN may no longer serve a finished week's projections
        "best": max(scored, sum(p["actual"] for p in best.values())),
        "bench_misses": sorted((p for i, p in best.items() if i not in started), key=lambda p: -p["actual"]),
        "bad_starts": sorted((p for p in starters if p["id"] not in best), key=lambda p: p["actual"]),
        "result": result,
    }


def recap_summary(team, week, facts):
    """The recap's sentences, one per line; the first line is the heading."""
    gap = facts["best"] - facts["scored"]
    lines = [f"{team_name(team)}: Week {week} recap"]
    if facts["result"]:
        mine, opp, theirs = facts["result"]
        verdict = "Won" if mine > theirs else "Lost" if mine < theirs else "Tied"
        lines.append(f"{verdict} {mine:g} to {theirs:g} against {opp}.")
    projected = f" (ESPN projected {facts['projected']:.1f})" if facts["projected"] else ""
    lines.append(f"Your starters scored {facts['scored']:.1f}{projected}.")
    if gap < 0.05:
        lines.append("That was the best lineup you could have fielded.")
        return lines
    lines.append(f"The best lineup in hindsight scored {facts['best']:.1f}: {gap:.1f} points were left on the bench.")
    if facts["bench_misses"]:
        lines.append("Should have started: " + ", ".join(f"{p['name']} ({p['actual']:.1f})" for p in facts["bench_misses"]))
    if facts["bad_starts"]:
        lines.append("Started instead: " + ", ".join(f"{p['name']} ({p['actual']:.1f})" for p in facts["bad_starts"]))
    return lines


def recap_rows(facts):
    return [
        (SLOTS.get(p["slot"], "?"), p["name"], f"{p['actual']:.1f}", f"{p['proj']:.1f}" if facts["projected"] else "-")
        for p in facts["starters"]
    ]


def recap_email(team, week, facts):
    lines, rows = recap_summary(team, week, facts), recap_rows(facts)
    cell = "<td style='padding:4px 8px'>{}</td>"
    body = (
        "<html><body style='font-family:sans-serif'>"
        + "".join(f"<p>{html.escape(ln)}</p>" for ln in lines)
        + "<table style='border-collapse:collapse' border='1'>"
        + "<tr><th>Slot</th><th>Player</th><th>Actual</th><th>Projected</th></tr>"
        + "".join("<tr>" + "".join(cell.format(html.escape(c)) for c in row) + "</tr>" for row in rows)
        + "</table></body></html>"
    )
    text = "\n".join(
        [lines[0], ""]
        + lines[1:]
        + ["", "STARTERS (slot, player, actual / projected):"]
        + [f"  {slot:<6} {name:<24} {actual:>5} / {proj:>5}" for slot, name, actual, proj in rows]
    )
    return body, text


def recap_run(season, sb, test):
    """Tuesday: how last week went. Waits (and does nothing) until every game of the week is final."""
    week = recap_week(sb)
    if not week or week < 1:
        return {"status": "no week to recap"}
    wsb = (
        sb
        if week == sb.get("week", {}).get("number")
        else fetch(f"scoreboard?dates={season}&seasontype={REGULAR_SEASON}&week={week}")
    )
    games = parse_games(wsb)
    if not test and (not games or not {g["id"] for g in games} <= set(final_winners(wsb))):
        log.info("week %s isn't over yet; no recap", week)
        return {"status": "week not over", "week": week, "run": "recap"}
    sent_weeks = load_param(f"{RECAP_PARAM}/{season}", [])
    if week in sent_weeks and not test:  # the Wednesday run is only a retry for a Tuesday that wasn't ready
        log.info("week %s recap already sent", week)
        return {"status": "already sent", "week": week, "run": "recap"}
    league, swid = fetch_league(season, week, matchups=True)
    team = my_team(league, swid)
    slots = lineup_slots(league)
    roster = players(team, games, week)
    facts = recap_facts(roster, slots, matchup_result(league, team, week))
    body, text = recap_email(team, week, facts)
    deliver(f"{'[TEST] ' if test else ''}Fantasy Week {week} Recap", body, text)
    if not test:
        try:
            save_param(f"{RECAP_PARAM}/{season}", sorted({*sent_weeks, week}))
        except Exception:  # the email went out; a missed save only risks a second recap
            log.exception("couldn't record the recap")
    log.info("sent the week %s recap: %.1f scored, %.1f best", week, facts["scored"], facts["best"])
    return {"status": "sent", "week": week, "run": "recap", "scored": facts["scored"], "best": facts["best"]}


def cookie_alert(exc):
    """Tell me the ESPN cookies need refreshing (at most once a day) instead of failing every run."""
    try:
        last = datetime.fromisoformat(load_param(COOKIE_ALERT_PARAM, ""))
    except ValueError:  # never warned, or a hand-edited value
        last = None
    if last and datetime.now(UTC) - last < COOKIE_ALERT_GAP:
        log.warning("ESPN cookies still rejected; already warned at %s", last)
        return {"status": "espn cookies still expired"}
    sent = notify(
        "nfl-fantasy-lineup: ESPN cookies expired - no lineup email",
        f"{exc}. The espn_s2 and SWID cookies have probably expired, so no fantasy email can be built.\n\n"
        "Fix: log in to fantasy.espn.com, copy the espn_s2 and SWID cookies (browser dev tools > Application > Cookies),\n"
        "then update both SSM parameters in us-east-2:\n"
        "  aws ssm put-parameter --name /nfl-pickem/espn-s2 --type SecureString --overwrite --value '<espn_s2>'\n"
        "  aws ssm put-parameter --name /nfl-pickem/espn-swid --type SecureString --overwrite --value '<{SWID}>'\n\n"
        "You'll get at most one of these emails a day until it's fixed.",
    )
    if not sent:
        raise exc  # no way to tell you, so fail loudly and let the error alarm do it
    try:
        save_param(COOKIE_ALERT_PARAM, datetime.now(UTC).isoformat())
    except Exception:  # the warning went out; a missed save only risks a repeat
        log.exception("couldn't save the cookie alert time")
    return {"status": "espn cookies expired"}


def handler(event, context):
    try:
        result = run_lineup(event)
    except EspnCookieError as exc:
        log.exception("ESPN cookies rejected")
        result = cookie_alert(exc)
        if result["status"] == "espn cookies still expired":
            return result  # no heartbeat: the watchdog keeps reminding me at each check until the cookies are fixed
    except Exception:
        heartbeat(FUNCTION, event)  # already reported by the error alarm
        raise
    heartbeat(FUNCTION, event)
    return result


def run_lineup(event):
    event = event or {}
    now_ct = datetime.now(CENTRAL)
    run = event.get("run") or {1: "waivers", 3: "thursday", 5: "saturday"}.get(now_ct.weekday(), "sunday")
    if run not in RUNS:
        raise ValueError(f"unknown run {run!r}; expected one of {sorted(RUNS)}")
    test = bool(event.get("test"))

    sb = fetch("scoreboard")
    if sb.get("season", {}).get("type") != REGULAR_SEASON:
        log.info("not the regular season (%s); no email", sb.get("season"))
        return {"status": "off season"}
    season = sb.get("season", {}).get("year", now_ct.year)
    week = sb.get("week", {}).get("number")
    if run == "recap":
        return recap_run(season, sb, test)
    games = parse_games(sb)
    if run == "alert" and not test and not any(kicks_soon(g, now_ct) for g in games):
        log.info("alert: no game kicks off in the next %d min, skipping", ALERT_WINDOW // 60)
        return {"status": "no game soon", "run": run}

    if run == "waivers" and games and all(g["started"] for g in games):
        week += 1  # ESPN still reports the finished week; roster, projections and byes should be for the next one
    league, swid = fetch_league(season, week)
    team = my_team(league, swid)
    slots = lineup_slots(league)
    if run == "waivers":
        return waiver_run(season, week, team, slots, test)
    roster = players(team, games, week)
    lineup = best_lineup(roster, slots)
    notes, reasons, summary = [], {}, ""

    if run == "alert" and not test and not moves(roster, lineup, run, now_ct):
        # Nothing to change on projections alone, so skip the AI review and the email.
        forget_if_settled(season, week, roster, lineup)
        log.info("week %s alert: lineup is still right, no email", week)
        return {"status": "no changes", "week": week, "run": run}

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
    actions = moves(roster, lineup, run, now_ct)
    if run == "alert" and not test:
        if not actions:  # the AI review can undo a projection-only change
            forget_if_settled(season, week, roster, lineup)
            log.info("week %s alert: lineup is still right, no email", week)
            return {"status": "no changes", "week": week, "run": run}
        if not alert_is_new(season, week, actions):  # you were already told; an ignored move shouldn't repeat
            log.info("week %s alert: same moves as the last alert, no email", week)
            return {"status": "unchanged", "week": week, "run": run}
    if run == "alert":
        notes.append("Late check before kickoff; only players in games starting soon are listed.")

    pickups, pickup_summary = None, ""  # None = pickups section failed, leave it out
    if run != "alert":
        try:
            pickups, pickup_summary, pickup_note = suggest_pickups(season, week, team, games, slots)
            notes.append(pickup_note)
        except Exception as exc:  # the lineup email still goes out
            log.exception("pickups failed")
            notes.append(f"Free-agent pickups failed ({type(exc).__name__}: {exc}).")

    label = {"thursday": "Thursday", "saturday": "Saturday preview", "sunday": "Sunday final", "alert": "Lineup alert"}[run]
    subject = f"{'[TEST] ' if test else ''}Fantasy Week {week} Lineup - {label}"
    send_email(subject, team, roster, lineup, actions, notes, summary, reasons, pickups, pickup_summary)
    if run == "alert" and not test:  # after the send: a failed email must not hide the alert from the retry
        remember_alert(season, week, actions)
    log.info("sent lineup for week %s (%s): %d moves", week, run, len(actions))
    return {
        "status": "sent",
        "week": week,
        "run": run,
        "moves": actions,
        "lineup": {k: (p["name"] if p else None) for k, p in lineup.items()},
        "pickups": [f"add {a['name']}, drop {d['name']}" for a, d, _ in pickups or []],
    }
