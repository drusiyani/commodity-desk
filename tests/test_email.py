"""The weekly email (email_report.py): when it sends, what it contains, the calendar parsers, and that a failed send
fails the run. No network and no real SMTP."""
import datetime as dt

import pytest

import email_report as R

MON_SUMMER_0600_UTC = int(dt.datetime(2026, 6, 8, 6, 0, tzinfo=dt.timezone.utc).timestamp())    # 07:00 BST
MON_WINTER_0700_UTC = int(dt.datetime(2026, 12, 7, 7, 0, tzinfo=dt.timezone.utc).timestamp())   # 07:00 GMT


def test_sends_only_at_seven_on_monday_uk_time():
    assert R.due(MON_SUMMER_0600_UTC) and R.due(MON_WINTER_0700_UTC)
    assert not R.due(MON_SUMMER_0600_UTC + 3600)           # 08:00 BST: the second scheduled run stays quiet
    assert not R.due(MON_WINTER_0700_UTC - 3600)           # 06:00 GMT
    assert not R.due(MON_WINTER_0700_UTC + 86400) and R.due(MON_WINTER_0700_UTC + 86400, force=True)


def fixture(now):
    h, day = 3600, 86400
    bars = lambda px: [{"time": now - 8 * day + k * h, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px * (1 + k / 4000)} for k in range(190)]
    eq = [{"time": now - 8 * day + k * day, "equity": 100_000 + 200 * k, "active_net": 60_000 + 150 * k, "benchmark": 100_000 + 100 * k,
           "quant": 100_000 - 50 * k, "kronos": 100_000} for k in range(9)]
    trades = [{"t": now - 3 * day, "symbol": "GC=F", "name": "Gold", "action": "SHORT", "side": "short", "source": "claude", "price": 4200,
               "stop": 4300, "target": 4000, "thesis": "Overbought into the Fed", "opposite": "Dollar could slide", "confidence": 61},
              {"t": now - day, "symbol": "GC=F", "name": "Gold", "action": "COVER", "source": "target", "price": 4000, "pnl": 500.0, "r": 2.0}]
    return {"equity": eq, "trades": trades, "decisions": [], "calls": [], "status": {"prompt_version": 3, "model": "m", "fx": 1.3, "lock": {"locked": True}},
            "portfolio": {"cash": 50_000, "positions": {}, "core": {"cash": 0, "positions": {}}},
            "prices": {"GC=F": {"name": "Gold", "bars": bars(4000)}}, "history": {"markets": {"GC=F": {"stats": {"pos5": 0.8, "last": 4000, "ma200": 3500}}}},
            "lab_trades": [], "kronos_trades": [], "quant_trades": [], "lit_trades": [], "ki_trades": [],
            "quant": {"sleeves": {"tsmom": {"pnl": -10, "costs": 10, "pos": {"GC=F": 1}}}}, "kcalls": [], "kicalls": [],
            "lit_now": {"markets": {"ES=F": {"h1": [[now - 2 * day, 7000, 7100, 6900, 7050], [now - day, 7050, 7200, 7000, 7150]], "events": []}}},
            "journal": {"weeks": [{"reviews": [{"id": f"t{now - day}-GC=F", "verdict": "good_reasoning", "why": "The Fed did turn hawkish."}]}]},
            "lessons": {"t": now - day, "lessons": [{"id": "L1", "text": "Fade stretched metals", "support": 4}],
                        "changes": [{"id": "L1", "action": "new", "text": "Fade stretched metals"}]},
            "health": [{"t": now - day, "problems": ["news"], "missing": [], "kronos_skipped": ["NG=F"]}], "config": {"frozen_since": None},
            "index_5y": {"ES=F": {"pos5": 0.9, "above": True}},
            "runs": [{"name": "Run trader", "t": "2026-10-05T10:00:00Z", "url": "u"}],
            "events": {"from": dt.date(2026, 10, 12), "to": dt.date(2026, 10, 18), "notes": ["Couldn't read the USDA WASDE calendar this week."],
                       "events": [{"day": dt.date(2026, 10, 14), "time": "10:30 ET", "what": "EIA Weekly Petroleum Status Report", "source": "EIA"}]}}


def test_the_review_has_every_section():
    now = MON_WINTER_0700_UTC
    subject, body, imgs = R.build(fixture(now), now)
    assert subject.startswith("Argon weekly review")
    for text in ("Markets this week", "Gold", "S&amp;P 500", "80%", "above", "The race", "Claude&#x27;s active 60%", "Quant bot",
                 "Closed short Gold", "Overbought into the Fed", "Dollar could slide", "61/100", "good reasoning", "+2.00R",
                 "Fade stretched metals", "Changed this week", "Which inputs actually help", "Report card", "Time-series momentum",
                 "Success criteria", "Problems this week", "failed: news", "Kronos skipped NG=F", "Workflow run failed: Run trader",
                 "Next week", "EIA Weekly Petroleum Status Report", "WASDE"):
        assert text in body, text


def test_a_failed_send_fails_the_run():
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["where"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = u
        def sendmail(self, frm, to, msg): sent["to"] = to

    msg = R.message("s", "<p>hi</p>", {"race": b"\x89PNG"}, "me@icloud.com", "a@x.com, b@y.com")
    R.send(msg, "me@icloud.com", "pw", "a@x.com, b@y.com", smtp=FakeSMTP)
    assert sent == {"where": ("smtp.mail.me.com", 587), "tls": True, "login": "me@icloud.com", "to": ["a@x.com", "b@y.com"]}
    assert 'Content-ID: <race>' in msg.as_string()

    class Broken(FakeSMTP):
        def login(self, u, p): raise R.smtplib.SMTPAuthenticationError(535, b"bad password")
    with pytest.raises(R.smtplib.SMTPAuthenticationError):
        R.send(msg, "me@icloud.com", "pw", "a@x.com", smtp=Broken)


def test_missing_secrets_stop_the_run(monkeypatch):
    monkeypatch.setenv("FORCE", "true")
    for k in ("EMAIL_USERNAME", "EMAIL_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        R.main()


def test_calendar_parsers():
    start, end = dt.date(2026, 10, 12), dt.date(2026, 10, 18)
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=US-Eastern:20261014T083000\nSUMMARY:Consumer Price Index for September 2026\nEND:VEVENT\n"
           "BEGIN:VEVENT\nDTSTART:20261015T083000\nSUMMARY:Real Earnings\nEND:VEVENT\n"
           "BEGIN:VEVENT\nDTSTART:20261106T083000\nSUMMARY:The Employment Situation for October\nEND:VEVENT\nEND:VCALENDAR")
    assert [(x["day"], x["time"]) for x in R.bls_events(start, end, ics)] == [(dt.date(2026, 10, 14), "08:30 ET")]
    fed = ('<h4>2026 FOMC Meetings</h4><div class="fomc-meeting__month"><strong>October</strong></div>'
           '<div class="fomc-meeting__date">13-14</div><div class="fomc-meeting__month"><strong>December</strong></div>'
           '<div class="fomc-meeting__date">8-9</div>')
    assert [x["day"] for x in R.fomc_events(start, end, fed)] == [dt.date(2026, 10, 14)]
    assert [x["day"] for x in R.wasde_events(start, end, "Release dates: Sep. 11, 2026 Oct. 15, 2026 Nov. 10, 2026")] == [dt.date(2026, 10, 15)]
    weekly = R.weekly_events(start, end)
    assert {(x["day"].weekday(), x["what"][:8]) for x in weekly} == {(2, "EIA Week"), (3, "EIA Week"), (0, "USDA Cro")}
    assert R.next_week(MON_WINTER_0700_UTC) == (dt.date(2026, 12, 7), dt.date(2026, 12, 13))
