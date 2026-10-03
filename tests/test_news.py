"""More headlines per market, no duplicates, and Claude only commenting on what's new."""
import json

import engine as E
from conftest import T0, closes

NOW = T0 + 10 * 3600
WORDS = ("rally miners vault bullion jewellery india etf inflows dollar yields tariffs mint auction refinery "
         "smuggling swiss exports bonds hedge funds options record slump sanctions reserves").split()


def distinct(i, prefix=""):
    """A headline that shares no story with distinct(j) for any other j."""
    return f"{prefix}{WORDS[i % len(WORDS)]} {WORDS[(i * 5 + 3) % len(WORDS)]} story{i} item{i}x"


def rss(*titles, source="Reuters", hours_ago=1):
    items = "".join(f"<item><title>{t} - {source}</title><source>{source}</source><link>https://x/{i}</link>"
                    f"<pubDate>Tue, 14 Nov 2023 {22 - hours_ago - i % 3:02d}:13:20 GMT</pubDate></item>"
                    for i, t in enumerate(titles))
    return f"<rss><channel>{items}</channel></rss>".encode()


def item(title, hours_ago=1, source="Reuters"):
    return {"title": title, "source": source, "link": "https://x", "published": NOW - hours_ago * 3600}


def test_parse_rss_strips_the_source():
    out = E.parse_rss(rss("Gold hits record as dollar slips"))
    assert out[0]["title"] == "Gold hits record as dollar slips" and out[0]["source"] == "Reuters"
    assert out[0]["published"] > 0


def test_same_story():
    assert E.same_story("Gold hits record high as dollar slips", "Gold hits record high as dollar slips!")
    assert E.same_story("Gold hits record high as dollar slips", "Gold hits a record high as the dollar slips again")
    assert not E.same_story("Gold hits record high as dollar slips", "Copper falls on weak China demand")
    assert E.news_id("Oil rises 2%") == E.news_id("oil rises 2%!")


def test_pick_headlines_takes_turns_between_searches_and_skips_repeats():
    a = [item(distinct(i, "A "), hours_ago=i) for i in range(10)]
    b = [item(a[0]["title"])] + [item(distinct(i + 50, "B "), hours_ago=i) for i in range(10)]
    picked = E.pick_headlines([a, b], n=8)
    titles = [p["title"] for p in picked]
    assert len(picked) == 8 and len(set(titles)) == 8
    assert sum(t.startswith("B ") for t in titles) >= 3   # the second search gets its share
    assert titles.count(a[0]["title"]) == 1               # the story both searches found is kept once


def test_merge_keeps_ids_and_claudes_comments():
    first = E.merge_news([], [("GC=F", item("Gold hits record high"))], NOW)
    first[0]["claude"] = {"impact": "bullish", "take": "Supportive."}
    again = E.merge_news(first, [("GC=F", item("Gold hits record high")), ("GC=F", item("Fed holds rates", 0))], NOW + 3600)
    assert len(again) == 2
    gold = next(n for n in again if n["title"] == "Gold hits record high")
    assert gold["id"] == first[0]["id"] and gold["claude"]["take"] == "Supportive."
    assert E.new_headlines(again) == [n for n in again if n["title"] == "Fed holds rates"]


def test_story_in_two_markets_is_shared_not_repeated():
    out = E.merge_news([], [("CL=F", item("Oil jumps as OPEC cuts output")), ("BZ=F", item("Oil jumps as OPEC cuts output"))], NOW)
    assert len(out) == 1 and out[0]["symbols"] == ["CL=F", "BZ=F"]


def test_old_headlines_age_out_and_old_files_upgrade():
    old_file = [{"symbol": "GC=F", "title": "Gold slips", "source": "x", "link": "", "published": NOW - 3600, "id": "n0"},
                {"symbol": "GC=F", "title": "Ancient news", "source": "x", "link": "", "published": NOW - 99 * 3600, "id": "n1"}]
    out = E.merge_news(old_file, [], NOW)
    assert [n["title"] for n in out] == ["Gold slips"]
    assert out[0]["id"] == E.news_id("Gold slips") and out[0]["symbols"] == ["GC=F"]


def test_per_market_cap():
    fresh = [("GC=F", item(distinct(i), hours_ago=i % 50)) for i in range(40)]
    out = E.merge_news([], fresh, NOW)
    assert len(out) == E.NEWS_MAX_PER_MARKET


def test_get_news_survives_a_failed_search(monkeypatch):
    calls = []

    def fake(q):
        calls.append(q)
        if "OPEC" in q:
            raise OSError("down")
        return E.parse_rss(rss(f"{q} headline one", f"{q} other story two"))
    monkeypatch.setattr(E, "fetch_rss", fake)
    news, ok = E.get_news([], NOW)
    assert ok and len(calls) == 2 * len(E.COMMODITIES)
    assert all(n["symbols"] for n in news)


def test_claude_is_only_sent_new_headlines(monkeypatch):
    seen = {}

    class Reply:
        status_code = 200
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": json.dumps({"summary": "ok", "news": []})}]}

    def post(url, timeout, headers, json):
        seen["prompt"] = json["messages"][0]["content"]
        return Reply()
    monkeypatch.setattr(E.requests, "post", post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    news = E.merge_news([], [("GC=F", item("Gold hits record high")), ("GC=F", item("Fed holds rates steady", 0))], NOW)
    old = next(n for n in news if n["title"] == "Gold hits record high")
    old["claude"] = {"impact": "bullish", "take": "Supportive."}
    prices = {"GC=F": {"name": "Gold", "group": "Metals", "exchange": "COMEX", "unit": "$", "bars": closes([2000] * 30)}}
    pf = E.new_portfolio()
    E.ask_claude(prices, {}, news, pf, {"GC=F": 2000.0}, 1.25, [], [], {}, {})
    p = seen["prompt"]
    new = next(n for n in news if n["title"] == "Fed holds rates steady")
    assert f"NEW [{new['id']}] Fed holds rates steady" in p
    assert f"[{old['id']}]" not in p                       # not sent again for a comment...
    assert "Earlier headline: Gold hits record high (you called it bullish)" in p  # ...just a reminder
    assert "(1 of them)" in p
