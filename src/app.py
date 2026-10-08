"""NFL Pick'em emailer.

Thursday run: picks the Thursday game(s) and locks their confidence points.
Sunday run:   picks every game that hasn't kicked off yet, using the points
              left over after the Thursday lock.

Confidence points follow the usual pick'em rule: with N games in the week,
each value 1..N is used exactly once.
"""

import json
import logging
import math
import os
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from pydantic import BaseModel

log = logging.getLogger()
log.setLevel(logging.INFO)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_CODES = {"WSH": "WAS", "JAX": "JAC"}  # ESPN abbreviation -> Kalshi ticker code
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
ODDS_API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
UA = {"User-Agent": "nfl-pickem-lambda"}
NFL_MARGIN_SD = 13.5  # std dev of NFL final margin vs. the spread, for spread -> win prob
REGULAR_SEASON = 2  # ESPN season types: 1 preseason, 2 regular, 3 postseason
CENTRAL = ZoneInfo("America/Chicago")
EASTERN = ZoneInfo("America/New_York")

RECIPIENT = os.environ["RECIPIENT_EMAIL"]
SENDER = os.environ["SENDER_EMAIL"]
USE_AI = os.environ.get("USE_AI", "true").lower() == "true"
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "medium")
STATE_PREFIX = os.environ.get("STATE_PARAM_PREFIX", "/nfl-pickem/locked")
ODDS_KEY_PARAM = os.environ.get("ODDS_API_KEY_PARAM", "/nfl-pickem/odds-api-key")
REGION = os.environ.get("AWS_REGION", "us-east-2")

ssm = boto3.client("ssm")
ses = boto3.client("sesv2")


# ---------------------------------------------------------------- ESPN data

def fetch(path):
    req = urllib.request.Request(f"{ESPN}/{path}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def parse_games(scoreboard):
    games = []
    for ev in scoreboard.get("events", []):
        comp = ev["competitions"][0]
        status = ev["status"]["type"]
        if status.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            continue
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = teams["home"], teams["away"]
        odds = (comp.get("odds") or [{}])[0]

        # Express the line from the favorite's side: fav_abbr, fav_by (points).
        fav_abbr, fav_by = None, 0.0
        spread = odds.get("spread")
        if spread is not None:
            if (odds.get("homeTeamOdds") or {}).get("favorite"):
                fav_abbr = home["team"]["abbreviation"]
            elif (odds.get("awayTeamOdds") or {}).get("favorite"):
                fav_abbr = away["team"]["abbreviation"]
            else:  # spread is quoted from the home side
                fav_abbr = home["team"]["abbreviation"] if spread < 0 else away["team"]["abbreviation"]
            fav_by = abs(float(spread))

        kickoff = datetime.strptime(ev["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        games.append({
            "id": ev["id"],
            "kickoff": kickoff,
            "kickoff_ct": kickoff.astimezone(CENTRAL),
            "started": status.get("state") != "pre",
            "home": home["team"]["abbreviation"],
            "away": away["team"]["abbreviation"],
            "home_name": home["team"]["displayName"],
            "away_name": away["team"]["displayName"],
            "home_record": (home.get("records") or [{}])[0].get("summary", ""),
            "away_record": (away.get("records") or [{}])[0].get("summary", ""),
            "line": odds.get("details", "no line"),
            "over_under": odds.get("overUnder"),
            "fav": fav_abbr,
            "fav_by": fav_by,
        })
    games.sort(key=lambda g: g["kickoff"])
    return games


def injuries_by_team(teams):
    """Non-active injury designations for the given team abbreviations."""
    out = {t: [] for t in teams}
    recent_ir = datetime.now(timezone.utc) - timedelta(days=14)
    for team in fetch("injuries").get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            abbr = ath.get("team", {}).get("abbreviation")
            status = inj.get("status", "")
            if abbr not in out or status == "Active":
                continue
            # Long-term IR players are already priced in; keep only fresh ones.
            if status == "Injured Reserve":
                try:
                    when = datetime.strptime(inj["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    continue
                if when < recent_ir:
                    continue
            details = inj.get("details") or {}
            out[abbr].append(
                f"{ath.get('displayName')} ({ath.get('position', {}).get('abbreviation', '?')}) "
                f"- {status}, {details.get('type', '')}: {inj.get('shortComment', '')}".strip()
            )
    return out


def recent_transactions(teams, days=10):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for tx in fetch("transactions").get("transactions", []):
        abbr = tx.get("team", {}).get("abbreviation")
        try:
            when = datetime.strptime(tx["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if abbr in teams and when >= cutoff:
            out.append(f"{when:%m/%d} {abbr}: {tx.get('description', '')}")
    return out


def recent_news(days=5):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for art in fetch("news?limit=50").get("articles", []):
        try:
            when = datetime.fromisoformat(art["published"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when >= cutoff:
            out.append(f"{when:%m/%d} {art.get('headline', '')} - {art.get('description', '')}")
    return out


# ------------------------------------------------------ betting markets

def kalshi_markets():
    markets, cursor = [], ""
    while True:
        query = urllib.parse.urlencode({"series_ticker": "KXNFLGAME", "status": "open",
                                        "limit": 200, "cursor": cursor})
        req = urllib.request.Request(f"{KALSHI}?{query}", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.load(resp)
        markets += page.get("markets", [])
        cursor = page.get("cursor")
        if not cursor:
            return markets


def add_kalshi(games):
    """Attach Kalshi's game-winner market price as home_prob. Returns games matched.

    Each game is an event KXNFLGAME-<yyMONdd ET><away><home> with one
    "<team> wins" market per side; the price midpoint is that side's probability.
    """
    quotes = {}
    for m in kalshi_markets():
        try:
            bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < bid <= ask and ask - bid <= 0.10:  # skip thin markets
            quotes[m["ticker"]] = (bid + ask) / 2

    matched = 0
    for g in games:
        if "home_prob" in g:
            continue
        day = g["kickoff"].astimezone(EASTERN)
        home, away = (KALSHI_CODES.get(g[s], g[s]) for s in ("home", "away"))
        event = f"KXNFLGAME-{day:%y}{MONTHS[day.month - 1]}{day:%d}{away}{home}"
        h, a = quotes.get(f"{event}-{home}"), quotes.get(f"{event}-{away}")
        if h is None or a is None:
            continue
        g["home_prob"] = h / (h + a)
        g["prob_source"] = "Kalshi market"
        matched += 1
    return matched


def odds_api_key():
    try:
        return ssm.get_parameter(Name=ODDS_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def implied(american):
    return 100 / (american + 100) if american > 0 else -american / (100 - american)


def add_consensus(games, key):
    """Attach the multi-sportsbook consensus from The Odds API to games without a price yet.

    home_prob is the no-vig moneyline win probability averaged across books;
    the line becomes the median spread. Returns the number of games matched.
    """
    query = urllib.parse.urlencode({"apiKey": key, "regions": "us", "markets": "h2h,spreads",
                                    "oddsFormat": "american"})
    req = urllib.request.Request(f"{ODDS_API}?{query}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        log.info("odds api credits remaining: %s", resp.headers.get("x-requests-remaining"))
        events = json.load(resp)

    matched = 0
    for g in games:
        if "home_prob" in g:
            continue
        home, away = g["home_name"], g["away_name"]
        ev = next((e for e in events if e["home_team"] == home and e["away_team"] == away
                   and abs(datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                           - g["kickoff"]) < timedelta(hours=12)), None)
        if not ev:
            continue
        probs, points = [], []
        for book in ev.get("bookmakers", []):
            for market in book.get("markets", []):
                out = {o["name"]: o for o in market.get("outcomes", [])}
                if home not in out or away not in out:
                    continue
                if market["key"] == "h2h":
                    h, a = implied(out[home]["price"]), implied(out[away]["price"])
                    probs.append(h / (h + a))
                elif market["key"] == "spreads" and "point" in out[home]:
                    points.append(out[home]["point"])
        if not probs:
            continue
        g["home_prob"] = sum(probs) / len(probs)
        g["prob_source"] = f"consensus of {len(probs)} sportsbooks"
        if points:
            spread = statistics.median(points)  # from the home side, negative = home favored
            g["fav"] = g["home"] if spread < 0 else g["away"] if spread > 0 else None
            g["fav_by"] = abs(spread)
            g["line"] = f"{g['fav']} {-abs(spread):g}" if g["fav"] else "PK"
        matched += 1
    return matched


def home_win_prob(g):
    """Market win probability, else estimated from the ESPN spread."""
    if "home_prob" in g:
        return g["home_prob"]
    if not g["fav"]:
        return 0.5
    fav_p = 0.5 * (1 + math.erf(g["fav_by"] / (NFL_MARGIN_SD * math.sqrt(2))))
    return fav_p if g["fav"] == g["home"] else 1 - fav_p


# ---------------------------------------------------------- locked state

def state_name(season, week):
    return f"{STATE_PREFIX}/{season}-w{week}"


def load_locked(season, week):
    try:
        val = ssm.get_parameter(Name=state_name(season, week))["Parameter"]["Value"]
        return json.loads(val)
    except ssm.exceptions.ParameterNotFound:
        return {}


def save_locked(season, week, locked):
    ssm.put_parameter(Name=state_name(season, week), Value=json.dumps(locked),
                      Type="String", Overwrite=True)


# ------------------------------------------------------------ pick logic

class Pick(BaseModel):
    game_id: str
    winner: str  # team abbreviation
    confidence: int
    reasoning: str


class PickSheet(BaseModel):
    picks: list[Pick]
    summary: str


def spread_picks(games, values):
    """No-AI picks: take the likelier winner, more points on bigger favorites."""
    def pick_prob(g):
        p = home_win_prob(g)
        return max(p, 1 - p)

    ranked = sorted(games, key=pick_prob)  # closest game -> fewest points
    picks = []
    for g, pts in zip(ranked, sorted(values)):
        p = home_win_prob(g)
        winner = g["home"] if p >= 0.5 else g["away"]  # dead even -> home team
        if "prob_source" in g:
            why = f"{max(p, 1 - p):.0%} to win ({g['prob_source']})"
        elif g["fav"]:
            why = f"Favored by {g['fav_by']:g} (ESPN line)"
        else:
            why = "No line posted; home team"
        picks.append(Pick(game_id=g["id"], winner=winner, confidence=pts, reasoning=why))
    return picks


def build_prompt(games, values, injuries, transactions, news, locked_note):
    lines = [
        "You are making NFL pick'em selections with confidence points.",
        f"Assign each game below exactly one confidence value from this set, using every value once: "
        f"{sorted(values)}. Higher = more confident the pick wins straight up (no spread).",
        "Weigh the betting line heavily, then adjust for injuries (especially QB, OL, and pass rush), "
        "roster moves, and news. Give a one- or two-sentence reason per pick.",
    ]
    if locked_note:
        lines.append(locked_note)
    lines.append("\nGAMES:")
    for g in games:
        lines.append(
            f"- game_id={g['id']}: {g['away_name']} ({g['away']}, {g['away_record']}) at "
            f"{g['home_name']} ({g['home']}, {g['home_record']}); kickoff {g['kickoff_ct']:%a %m/%d %I:%M %p} CT; "
            f"line {g['line']}; O/U {g['over_under']}"
            + (f"; {g['prob_source']}: {g['home']} {g['home_prob']:.0%} to win" if "home_prob" in g else "")
        )
    lines.append("\nINJURY REPORT (non-active designations):")
    for team, items in injuries.items():
        if items:
            lines.append(f"{team}: " + "; ".join(items))
    if transactions:
        lines.append("\nRECENT TRANSACTIONS:")
        lines.extend(transactions)
    if news:
        lines.append("\nRECENT NFL NEWS:")
        lines.extend(news)
    lines.append("\nIn `winner`, use the team abbreviation exactly as given.")
    return "\n".join(lines)


def ai_picks(games, values, locked_note):
    from anthropic import AnthropicBedrockMantle

    teams = {g["home"] for g in games} | {g["away"] for g in games}
    prompt = build_prompt(games, values, injuries_by_team(teams),
                          recent_transactions(teams), recent_news(), locked_note)

    # Bedrock doesn't support structured outputs, so the sheet comes back as the
    # input of a tool call and is validated with pydantic here.
    client = AnthropicBedrockMantle(aws_region=REGION)
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=16000,
        output_config={"effort": EFFORT},
        tools=[{
            "name": "submit_picks",
            "description": "Submit the finished pick sheet: one pick per game plus a short summary.",
            "input_schema": PickSheet.model_json_schema(),
        }],
        messages=[{"role": "user", "content": prompt + "\n\nSubmit your picks by calling submit_picks."}],
    )
    log.info("usage: %s", response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    call = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_picks"), None)
    if call is None:
        raise RuntimeError(f"model did not submit picks (stop_reason={response.stop_reason})")
    sheet = PickSheet.model_validate(call.input)
    return normalize(sheet.picks, games, values), sheet.summary


def normalize(picks, games, values):
    """Make the AI output a valid sheet: one pick per game, each value used once.

    Keeps the model's relative ordering but reassigns the actual numbers, so a
    duplicated or out-of-range value can't produce an invalid entry.
    """
    by_id = {g["id"]: g for g in games}
    seen = {}
    for p in picks:
        g = by_id.get(p.game_id)
        if g and p.game_id not in seen and p.winner in (g["home"], g["away"]):
            seen[p.game_id] = p
    # Any game the model skipped falls back to the spread pick.
    missing = [g for g in games if g["id"] not in seen]
    for p in spread_picks(missing, range(len(missing))):
        p.confidence = -1  # sort below everything the model ranked
        seen[p.game_id] = p
    ordered = sorted(seen.values(), key=lambda p: p.confidence)
    for p, pts in zip(ordered, sorted(values)):
        p.confidence = pts
    return ordered


# ---------------------------------------------------------------- email

def send_email(subject, picks, games, notes, summary):
    by_id = {g["id"]: g for g in games}
    rows, text_rows = [], []
    for p in sorted(picks, key=lambda p: -p.confidence):
        g = by_id[p.game_id]
        matchup = f"{g['away']} @ {g['home']}"
        when = f"{g['kickoff_ct']:%a %I:%M %p}"
        rows.append(
            f"<tr><td style='padding:4px 8px;text-align:right'><b>{p.confidence}</b></td>"
            f"<td style='padding:4px 8px'><b>{p.winner}</b></td>"
            f"<td style='padding:4px 8px'>{matchup}</td><td style='padding:4px 8px'>{when}</td>"
            f"<td style='padding:4px 8px'>{g['line']}</td><td style='padding:4px 8px'>{p.reasoning}</td></tr>"
        )
        text_rows.append(f"{p.confidence:>3}  {p.winner:<4} {matchup:<11} {when:<13} {g['line']:<10} {p.reasoning}")

    notes_html = "".join(f"<p style='color:#555'>{n}</p>" for n in notes)
    summary_html = f"<p>{summary}</p>" if summary else ""
    html = (
        f"<html><body style='font-family:sans-serif'>{summary_html}"
        "<table style='border-collapse:collapse' border='1'>"
        "<tr><th>Pts</th><th>Pick</th><th>Game</th><th>Kickoff (CT)</th><th>Line</th><th>Why</th></tr>"
        f"{''.join(rows)}</table>{notes_html}</body></html>"
    )
    text = "\n".join(([summary, ""] if summary else []) + text_rows + [""] + notes)

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
    run = event.get("run") or ("thursday" if now_ct.weekday() == 3 else "sunday")
    test = bool(event.get("test"))

    sb = fetch("scoreboard")
    if sb.get("season", {}).get("type") != REGULAR_SEASON:
        log.info("not the regular season (%s); no email", sb.get("season"))
        return {"status": "off season"}
    season = sb.get("season", {}).get("year", now_ct.year)
    week = sb.get("week", {}).get("number")
    games = parse_games(sb)
    if not games:
        log.info("no games on the scoreboard; nothing to do")
        return {"status": "no games"}

    all_values = set(range(1, len(games) + 1))
    locked = load_locked(season, week)
    notes = []

    # Win probabilities: Kalshi first, then The Odds API (if a key is set), else ESPN's line.
    unplayed = [g for g in games if not g["started"]]
    try:
        add_kalshi(unplayed)
    except Exception as exc:  # market outage still gets an email
        log.exception("kalshi failed")
        notes.append(f"Kalshi failed ({type(exc).__name__}: {exc}).")
    key = odds_api_key()
    if key and any("home_prob" not in g for g in unplayed):
        try:
            add_consensus(unplayed, key)
        except Exception as exc:
            log.exception("odds api failed")
            notes.append(f"Odds API failed ({type(exc).__name__}: {exc}).")
    no_market = [f"{g['away']}@{g['home']}" for g in unplayed if "home_prob" not in g]
    if no_market:
        notes.append(f"No market price for {', '.join(no_market)}; used the ESPN line.")

    if run == "thursday":
        targets = [g for g in games if g["kickoff_ct"].weekday() == 3
                   and not g["started"] and g["id"] not in locked]
        # Rank the whole week so the Thursday game gets a fair share of the points.
        pool = [g for g in games if not g["started"] and g["id"] not in locked]
    else:
        targets = [g for g in games if not g["started"] and g["id"] not in locked]
        pool = targets

    if not targets:
        log.info("no unplayed %s games in week %s", run, week)
        return {"status": "no target games"}

    available = all_values - {v["confidence"] for v in locked.values()}
    # Games that kicked off without a lock (picked by hand) used points we don't
    # know; assume they took the lowest leftovers so ours keep the high values.
    unknown = [g for g in games if g["started"] and g["id"] not in locked]
    if unknown:
        available = set(sorted(available)[len(available) - len(pool):])
        notes.append(f"{len(unknown)} game(s) already kicked off with no saved pick; "
                     f"assumed they used the lowest point values.")

    locked_note = ""
    if locked:
        used = ", ".join(f"{v['winner']} ({v['confidence']})" for v in locked.values())
        locked_note = f"Already locked earlier this week (not part of this set): {used}."

    summary = ""
    if USE_AI:
        try:
            picks, summary = ai_picks(pool, available, locked_note)
            notes.append(f"Picks by {MODEL_ID} on Amazon Bedrock.")
        except Exception as exc:  # any AI failure still gets an email
            log.exception("AI picks failed")
            picks = spread_picks(pool, available)
            notes.append(f"AI picks failed ({type(exc).__name__}: {exc}); used spread-based picks instead.")
    else:
        picks = spread_picks(pool, available)
        notes.append("Picks from betting odds (AI off).")

    target_ids = {g["id"] for g in targets}
    picks = [p for p in picks if p.game_id in target_ids]

    if run == "thursday" and not test:
        for p in picks:
            locked[p.game_id] = {"winner": p.winner, "confidence": p.confidence}
        save_locked(season, week, locked)

    label = "Thursday" if run == "thursday" else "Sunday/Monday"
    subject = f"{'[TEST] ' if test else ''}NFL Week {week} Pick'em - {label}"
    send_email(subject, picks, games, notes, summary)
    log.info("sent %d picks for week %s (%s)", len(picks), week, run)
    return {"status": "sent", "week": week, "run": run,
            "picks": [p.model_dump() for p in picks]}
