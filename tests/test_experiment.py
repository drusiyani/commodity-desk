"""The experiment: the results lock (real: locked results never reach the plain public files) and the success
criteria's progress."""
import json

import pytest

import engine as E
import experiment as X
import vault as V

DAY = 86400


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "DATA", tmp_path / "site" / "data")
    monkeypatch.setattr(E, "STATE", tmp_path / "state")
    monkeypatch.setenv("ARGON_PASSWORD", "test password")
    monkeypatch.setattr(V, "ITER", 1000)
    monkeypatch.setattr(V.encrypt, "__defaults__", (None, 1000))
    E.DATA.mkdir(parents=True)
    return tmp_path


def test_the_lock_date_is_midnight_on_new_year_uk_time():
    import datetime as dt
    from zoneinfo import ZoneInfo
    assert X.LOCK_UNTIL == int(dt.datetime(2027, 1, 1, tzinfo=ZoneInfo("Europe/London")).timestamp())
    assert X.locked(X.LOCK_UNTIL - 1) and not X.locked(X.LOCK_UNTIL)


def test_locked_results_are_only_saved_encrypted_and_published_after_the_date(dirs, monkeypatch):
    (E.DATA / "equity.json").write_text(json.dumps([{"time": 1, "equity": 100_000}]))   # public before the lock
    monkeypatch.setattr(X, "LOCK_UNTIL", 2_000_000_000)                                  # locked
    assert E.load("equity.json", []) == [{"time": 1, "equity": 100_000}]                 # migrated once
    E.save("equity.json", [{"time": 2, "equity": 101_000}])
    assert not (E.DATA / "equity.json").exists() and (E.STATE / "equity.json.enc").exists()
    assert "101000" not in (E.STATE / "equity.json.enc").read_text()                      # encrypted
    assert E.load("equity.json", []) == [{"time": 2, "equity": 101_000}]
    monkeypatch.setattr(X, "LOCK_UNTIL", 1)                                              # the date has passed
    rows = E.load("equity.json", [])
    assert rows == [{"time": 2, "equity": 101_000}]                                      # read from state/ once...
    E.save("equity.json", rows + [{"time": 3, "equity": 102_000}])
    assert json.loads((E.DATA / "equity.json").read_text())[-1]["equity"] == 102_000     # ...then public again
    assert not (E.STATE / "equity.json.enc").exists()
    assert E.hidden("trades.json") and not E.hidden("prices.json")                       # private files stay private


def test_progress_against_the_success_criteria():
    t0 = 1_791_000_000
    eq = [{"time": t0 + k * DAY, "equity": 100_000 * (1.001 ** k) * (1 + 0.003 * (-1) ** k),
           "active_net": 60_000 * (0.999 ** k), "benchmark": 100_000 * (1.0005 ** k) * (1 + 0.004 * (-1) ** k)} for k in range(60)]
    calls = [{"t": t0 + k * 3600, "checked": True, "right": k % 10 < 6} for k in range(250)]
    calls += [{"t": t0 - DAY, "checked": True, "right": False}] * 50                       # before the freeze: not counted
    p = X.progress(eq, calls, t0, t0 + 70 * DAY)
    by = {c["id"]: c for c in p["criteria"]}
    assert by["equity_return"]["met"] is True and by["active_net_return"]["met"] is False
    assert by["equity_sharpe"]["met"] is True and by["active_net_sharpe"]["met"] is False
    assert by["report"]["calls"] == 250 and by["report"]["value"] == pytest.approx(0.6) and by["report"]["met"] is True
    assert p["met"] is False and p["final"] is False
    few = X.progress(eq, calls[:150], t0, t0 + 70 * DAY)
    assert next(c for c in few["criteria"] if c["id"] == "report")["met"] is False          # fewer than 200 calls
    assert X.progress([], [], t0, t0)["criteria"][0]["met"] is None                         # nothing yet
