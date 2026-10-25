"""Pick'em logic: parsing, win probabilities, Kalshi matching and confidence points."""

from datetime import UTC, datetime

import pytest

import app


def event(eid, date, home, away, spread=None, home_fav=None, state="pre", status="STATUS_SCHEDULED"):
    odds = {}
    if spread is not None:
        odds = {"spread": spread, "details": "line", "overUnder": 44.5}
        if home_fav is not None:
            odds["homeTeamOdds" if home_fav else "awayTeamOdds"] = {"favorite": True}

    def team(abbr, ha, tid):
        return {"homeAway": ha, "team": {"abbreviation": abbr, "id": tid, "displayName": abbr.title()}}

    return {
        "id": eid,
        "date": date,
        "status": {"type": {"state": state, "name": status}},
        "competitions": [{"competitors": [team(home, "home", "1"), team(away, "away", "2")], "odds": [odds] if odds else []}],
    }


def game(gid, home, away, **kw):
    return {
        "id": gid,
        "home": home,
        "away": away,
        "fav": None,
        "fav_by": 0.0,
        "kickoff": datetime(2026, 10, 25, 17, tzinfo=UTC),
        **kw,
    }


class TestParseGames:
    def test_sorted_by_kickoff_and_favorite_from_odds(self):
        sb = {
            "events": [
                event("b", "2026-10-25T20:00Z", "DAL", "NYG", spread=-3, home_fav=True),
                event("a", "2026-10-25T17:00Z", "KC", "LV", spread=7, home_fav=False),
            ]
        }
        games = app.parse_games(sb)
        assert [g["id"] for g in games] == ["a", "b"]
        assert (games[0]["fav"], games[0]["fav_by"]) == ("LV", 7.0)
        assert (games[1]["fav"], games[1]["fav_by"]) == ("DAL", 3.0)

    def test_spread_quoted_from_home_side_when_no_favorite_flag(self):
        g = app.parse_games({"events": [event("a", "2026-10-25T17:00Z", "KC", "LV", spread=-2.5)]})[0]
        assert g["fav"] == "KC"
        g = app.parse_games({"events": [event("a", "2026-10-25T17:00Z", "KC", "LV", spread=2.5)]})[0]
        assert g["fav"] == "LV"

    def test_no_odds_and_skips_postponed_and_marks_started(self):
        sb = {
            "events": [
                event("a", "2026-10-25T17:00Z", "KC", "LV"),
                event("b", "2026-10-25T17:00Z", "DAL", "NYG", status="STATUS_POSTPONED"),
                event("c", "2026-10-25T17:00Z", "SF", "SEA", state="in"),
            ]
        }
        games = {g["id"]: g for g in app.parse_games(sb)}
        assert set(games) == {"a", "c"}
        assert games["a"]["fav"] is None and games["a"]["line"] == "no line"
        assert games["c"]["started"] and not games["a"]["started"]


class TestWinProbability:
    def test_market_probability_wins(self):
        assert app.home_win_prob(game("a", "KC", "LV", home_prob=0.7, fav="LV", fav_by=9)) == 0.7

    def test_pick_em_is_even(self):
        assert app.home_win_prob(game("a", "KC", "LV")) == 0.5

    def test_spread_converts_to_probability_for_either_side(self):
        home = app.home_win_prob(game("a", "KC", "LV", fav="KC", fav_by=7))
        away = app.home_win_prob(game("a", "KC", "LV", fav="LV", fav_by=7))
        assert home == pytest.approx(0.70, abs=0.02)
        assert home + away == pytest.approx(1)

    def test_implied_american_odds(self):
        assert app.implied(100) == 0.5
        assert app.implied(-200) == pytest.approx(2 / 3)
        assert app.implied(300) == pytest.approx(0.25)


class TestKalshi:
    def market(self, ticker, bid, ask):
        return {"ticker": ticker, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask)}

    def test_matches_game_and_normalizes_prices(self, monkeypatch):
        # 1pm ET kickoff on 2026-10-25; ESPN WSH/JAX are Kalshi WAS/JAC.
        g = game("a", "JAX", "WSH", kickoff=datetime(2026, 10, 25, 17, tzinfo=UTC))
        monkeypatch.setattr(
            app,
            "kalshi_markets",
            lambda: [
                self.market("KXNFLGAME-26OCT25WASJAC-JAC", 0.58, 0.62),
                self.market("KXNFLGAME-26OCT25WASJAC-WAS", 0.38, 0.42),
            ],
        )
        assert app.add_kalshi([g]) == 1
        assert g["home_prob"] == pytest.approx(0.6)
        assert g["prob_source"] == "Kalshi market"

    def test_evening_utc_kickoff_uses_eastern_date(self, monkeypatch):
        # 01:00Z on the 27th is 9pm ET on the 26th.
        g = game("a", "KC", "BUF", kickoff=datetime(2026, 10, 27, 1, tzinfo=UTC))
        monkeypatch.setattr(
            app,
            "kalshi_markets",
            lambda: [self.market("KXNFLGAME-26OCT26BUFKC-KC", 0.5, 0.52), self.market("KXNFLGAME-26OCT26BUFKC-BUF", 0.48, 0.5)],
        )
        assert app.add_kalshi([g]) == 1

    def test_thin_missing_or_one_sided_markets_are_skipped(self, monkeypatch):
        g = game("a", "KC", "LV", kickoff=datetime(2026, 10, 25, 17, tzinfo=UTC))
        monkeypatch.setattr(
            app,
            "kalshi_markets",
            lambda: [
                self.market("KXNFLGAME-26OCT25LVKC-KC", 0.40, 0.70),  # spread too wide
                self.market("KXNFLGAME-26OCT25LVKC-LV", 0.30, 0.32),
                {"ticker": "KXNFLGAME-26OCT25XXYY-XX"},
            ],
        )  # no prices
        assert app.add_kalshi([g]) == 0
        assert "home_prob" not in g

    def test_existing_price_is_kept(self, monkeypatch):
        g = game("a", "KC", "LV", home_prob=0.9)
        monkeypatch.setattr(app, "kalshi_markets", lambda: [])
        assert app.add_kalshi([g]) == 0 and g["home_prob"] == 0.9


class TestPicks:
    GAMES = [
        game("close", "KC", "LV", home_prob=0.52),
        game("lock", "SF", "SEA", home_prob=0.9),
        game("mid", "DAL", "NYG", home_prob=0.35),
    ]

    def test_spread_picks_give_most_points_to_biggest_favorite(self):
        picks = {p.game_id: p for p in app.spread_picks(self.GAMES, [1, 2, 3])}
        assert {p.confidence for p in picks.values()} == {1, 2, 3}
        assert (picks["lock"].confidence, picks["lock"].winner) == (3, "SF")
        assert (picks["mid"].confidence, picks["mid"].winner) == (2, "NYG")
        assert (picks["close"].confidence, picks["close"].winner) == (1, "KC")

    def test_dead_even_goes_to_home_team(self):
        (p,) = app.spread_picks([game("a", "KC", "LV")], [5])
        assert p.winner == "KC" and "No line" in p.reasoning

    def test_normalize_repairs_duplicate_and_missing_points(self):
        sheet = [
            app.Pick(game_id="lock", winner="SF", confidence=10, reasoning=""),
            app.Pick(game_id="mid", winner="NYG", confidence=10, reasoning=""),
        ]
        picks = app.normalize(sheet, self.GAMES, [1, 2, 3])
        assert sorted(p.confidence for p in picks) == [1, 2, 3]
        assert {p.game_id for p in picks} == {"close", "lock", "mid"}
        assert picks[0].game_id == "close"  # skipped game falls to the bottom

    def test_normalize_drops_unknown_games_wrong_teams_and_repeats(self):
        sheet = [
            app.Pick(game_id="nope", winner="SF", confidence=1, reasoning=""),
            app.Pick(game_id="lock", winner="ZZZ", confidence=2, reasoning=""),
            app.Pick(game_id="mid", winner="DAL", confidence=3, reasoning=""),
            app.Pick(game_id="mid", winner="NYG", confidence=4, reasoning=""),
        ]
        picks = {p.game_id: p for p in app.normalize(sheet, self.GAMES, [1, 2, 3])}
        assert set(picks) == {"close", "lock", "mid"}
        assert picks["mid"].winner == "DAL"  # first answer for a game wins
        assert picks["lock"].winner == "SF"  # invalid pick replaced by the spread pick


def test_handler_notifies_and_reraises_on_failure(monkeypatch):
    published = []
    monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "arn:topic")
    monkeypatch.setattr(app.sns, "publish", lambda **kw: published.append(kw))
    monkeypatch.setattr(app, "run_pickem", lambda event: (_ for _ in ()).throw(RuntimeError("espn down")))
    with pytest.raises(RuntimeError):
        app.handler({"run": "sunday"}, None)
    assert published[0]["TopicArn"] == "arn:topic"
    assert "espn down" in published[0]["Message"]
    assert len(published[0]["Subject"]) <= 100


def test_notify_failure_is_noop_without_topic(monkeypatch):
    monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "")
    monkeypatch.setattr(app.sns, "publish", lambda **kw: pytest.fail("published"))
    app.notify_failure({}, RuntimeError("x"))


def test_notify_failure_swallows_publish_errors(monkeypatch):
    monkeypatch.setattr(app, "FAILURE_TOPIC_ARN", "arn:topic")
    monkeypatch.setattr(app.sns, "publish", lambda **kw: (_ for _ in ()).throw(OSError("sns down")))
    app.notify_failure({}, RuntimeError("x"))
