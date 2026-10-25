"""Live-data fetchers against canned ESPN / Kalshi / ESPN-fantasy responses."""

from datetime import UTC, datetime, timedelta

import app
import lineup


def stamp(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%MZ")


class TestEspnFeeds:
    def test_injuries_keep_non_active_and_fresh_ir_only(self, http):
        def inj(name, abbr, status, date=None):
            item = {
                "athlete": {"displayName": name, "team": {"abbreviation": abbr}, "position": {"abbreviation": "QB"}},
                "status": status,
                "details": {"type": "Knee"},
                "shortComment": "c",
            }
            if date:
                item["date"] = date
            return item

        http(
            {
                "injuries": {
                    "injuries": [
                        {
                            "injuries": [
                                inj("Healthy", "KC", "Active"),
                                inj("Out Guy", "KC", "Out"),
                                inj("Old IR", "KC", "Injured Reserve", stamp(60)),
                                inj("New IR", "KC", "Injured Reserve", stamp(2)),
                                inj("No Date IR", "KC", "Injured Reserve"),
                                inj("Other Team", "LV", "Out"),
                            ]
                        }
                    ]
                }
            }
        )
        out = app.injuries_by_team({"KC"})
        assert list(out) == ["KC"]
        assert [s.split(" (")[0] for s in out["KC"]] == ["Out Guy", "New IR"]

    def test_transactions_filter_by_team_and_age(self, http):
        http(
            {
                "transactions": {
                    "transactions": [
                        {"team": {"abbreviation": "KC"}, "date": stamp(1), "description": "Signed X"},
                        {"team": {"abbreviation": "KC"}, "date": stamp(30), "description": "Old"},
                        {"team": {"abbreviation": "LV"}, "date": stamp(1), "description": "Other"},
                        {"team": {"abbreviation": "KC"}, "description": "no date"},
                    ]
                }
            }
        )
        assert [t.split(": ")[1] for t in app.recent_transactions({"KC"})] == ["Signed X"]

    def test_news_keeps_recent_articles(self, http):
        new = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        old = (datetime.now(UTC) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
        http(
            {
                "news": {
                    "articles": [
                        {"published": new, "headline": "Fresh", "description": "d"},
                        {"published": old, "headline": "Stale"},
                        {"headline": "Undated"},
                    ]
                }
            }
        )
        assert [n.split(" ", 1)[1] for n in app.recent_news()] == ["Fresh - d"]


class TestKalshiFetch:
    def test_follows_the_cursor_until_exhausted(self, http):
        seen = http(
            {
                "kalshi": [
                    {"markets": [{"ticker": "A"}], "cursor": "next"},
                    {"markets": [{"ticker": "B"}], "cursor": ""},
                ]
            }
        )
        assert [m["ticker"] for m in app.kalshi_markets()] == ["A", "B"]
        assert "cursor=next" in seen[1] and "series_ticker=KXNFLGAME" in seen[0]


class TestOddsApi:
    def test_consensus_probability_and_median_spread(self, http, monkeypatch):
        kickoff = datetime(2026, 10, 25, 17, tzinfo=UTC)
        g = {
            "id": "1",
            "home": "KC",
            "away": "LV",
            "home_name": "Kansas City Chiefs",
            "away_name": "Las Vegas Raiders",
            "kickoff": kickoff,
        }

        def book(home_price, away_price, home_spread):
            return {
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": home_price},
                            {"name": "Las Vegas Raiders", "price": away_price},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "point": home_spread},
                            {"name": "Las Vegas Raiders", "point": -home_spread},
                        ],
                    },
                ]
            }

        http(
            {
                "the-odds-api": [
                    [
                        {
                            "home_team": "Kansas City Chiefs",
                            "away_team": "Las Vegas Raiders",
                            "commence_time": "2026-10-25T17:00:00Z",
                            "bookmakers": [book(-200, 170, -4.5), book(-220, 180, -5.5)],
                        }
                    ]
                ]
            }
        )
        assert app.add_consensus([g], "key") == 1
        assert 0.6 < g["home_prob"] < 0.72
        assert g["prob_source"] == "consensus of 2 sportsbooks"
        assert (g["fav"], g["fav_by"], g["line"]) == ("KC", 5.0, "KC -5")


class TestFantasyFetch:
    def setup_method(self):
        lineup.secret.cache_clear()

    def test_sends_cookies_and_filter_header(self, monkeypatch):
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return b'{"ok": 1}'

        def urlopen(req, timeout=None):
            sent.update(url=req.full_url, headers=dict(req.header_items()))
            return Resp()

        monkeypatch.setattr(lineup, "secret", lambda name: f"<{name}>")
        monkeypatch.setattr(lineup.urllib.request, "urlopen", urlopen)
        assert lineup.fantasy_get("2026?x=1", {"players": {}}) == {"ok": 1}
        assert sent["url"].endswith("/2026?x=1")
        assert "espn_s2=<" in sent["headers"]["Cookie"] and "SWID=<" in sent["headers"]["Cookie"]
        assert sent["headers"]["X-fantasy-filter"] == '{"players": {}}'

    def test_free_agents_and_pro_teams_unwrap_the_response(self, monkeypatch):
        calls = []

        def fake_get(path, player_filter=None):
            calls.append((path, player_filter))
            if "kona_player_info" in path:
                return {"players": [{"player": {"id": 1}}]}
            return {"settings": {"proTeams": [{"id": 12, "abbrev": "KC", "byeWeek": 10}, {"id": 0, "abbrev": "FA"}]}}

        monkeypatch.setattr(lineup, "fantasy_get", fake_get)
        assert lineup.fetch_free_agents(2026, 7) == [{"player": {"id": 1}}]
        assert calls[0][1]["players"]["filterStatus"]["value"] == ["FREEAGENT", "WAIVERS"]
        assert lineup.pro_teams(2026) == {12: ("KC", 10), 0: ("FA", None)}


class TestPlayerNews:
    def test_keeps_recent_rotowire_items_capped_at_three(self, http):
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        feed = [{"published": recent, "type": "Rotowire", "headline": f"H{i}", "story": "a  b\n c"} for i in range(5)]
        feed += [{"published": recent, "type": "Other", "headline": "skip"}]
        http({"news/players": {"feed": feed}})
        out = lineup.player_news(1)
        assert [n["headline"] for n in out] == ["H0", "H1", "H2"]
        assert out[0]["story"] == "a b c"

    def test_failure_returns_empty(self, http):
        http({"news/players": OSError("down")})
        assert lineup.player_news(1) == []
