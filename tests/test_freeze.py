"""The experiment freeze (config.json "frozen"): while it is on, Claude's prompt, the learning process and the live
strategy line-up must not change. The first test is the guard: it fails if any of them differs from what freeze.json
(and the line-up the first frozen run recorded in config.json) says. The others check the freeze machinery."""
import json

import pytest

import engine as E
import freeze
import research as RS

CONFIG = json.loads((freeze.ROOT / "config.json").read_text())


@pytest.mark.skipif(not CONFIG.get("frozen"), reason="the experiment isn't frozen")
def test_nothing_frozen_has_changed():
    want = json.loads((freeze.ROOT / "freeze.json").read_text())
    got = freeze.fingerprints()
    assert got["prompt_version"] == want["prompt_version"], "the prompt version changed during the freeze"
    assert got["prompt"] == want["prompt"], "Claude's prompt (or how its answer is used) changed during the freeze"
    assert got["learning"] == want["learning"], "the learning process (learning.py) changed during the freeze"
    if CONFIG.get("frozen_lineup") is not None:
        assert freeze.lineup() == CONFIG["frozen_lineup"], "the live strategy line-up changed during the freeze"


def test_the_fingerprint_notices_a_prompt_change(monkeypatch):
    before = freeze.fingerprints()["prompt"]
    assert freeze.fingerprints()["prompt"] == before                  # the same code gives the same fingerprint
    monkeypatch.setattr(E, "MIN_RR", 2.0)
    assert freeze.fingerprints()["prompt"] != before                  # any rule or prompt change shows up


def test_the_first_frozen_run_stamps_its_date_and_line_up(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(freeze, "lineup", lambda: {"portfolio": ["s12"], "weights": {"s12": 1.0}})
    cfg = {"frozen": True, "frozen_since": "set by the first run after merging"}
    assert E.frozen(cfg) == (True, None)
    assert E.start_freeze(cfg, 1_791_000_000) == (True, "2026-10-03")
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["frozen_since"] == "2026-10-03" and saved["frozen_lineup"] == {"portfolio": ["s12"], "weights": {"s12": 1.0}}
    (tmp_path / "config.json").unlink()
    assert E.start_freeze(saved, 1_799_000_000) == (True, "2026-10-03") and not (tmp_path / "config.json").exists()
    assert E.frozen({"frozen": False, "frozen_since": "2026-10-03"}) == (False, "2026-10-03")


def test_while_frozen_the_lab_keeps_testing_but_the_line_up_stays(monkeypatch):
    monkeypatch.setattr(E, "frozen", lambda config=None: (True, "2026-10-08"))
    monkeypatch.setattr(E, "load", lambda name, default: {})
    monkeypatch.setattr(RS, "allocate", lambda lab, data: pytest.fail("weights must not be recomputed while frozen"))
    lab = {"portfolio": ["s1"], "allocation": {"weights": {"s1": 1.0}},
           "strategies": [{"id": "s1", "passed": False, "score": 1}, {"id": "s2", "passed": True, "score": 5}]}
    RS.finish(lab, None)
    assert lab["portfolio"] == ["s1"] and lab["allocation"] == {"weights": {"s1": 1.0}}
    assert lab["strategies"][1]["waiting"] is True and lab["strategies"][0]["waiting"] is False
    assert lab["frozen"] == "2026-10-08"


def test_while_frozen_a_live_strategy_is_not_retired(monkeypatch):
    monkeypatch.setattr(E.V, "live_check", lambda r, st: {"retire": "live results far below the backtest"})
    st = {"id": "s1", "name": "A", "timeframe": "daily", "rules": {}}
    lb = E.new_lab_book(0)
    E.run_portfolio(lb, [st], {"s1": {}}, {}, {}, 1.0, 1, freeze=True)
    assert "s1" in lb["sleeves"] and not lb["strategies"]["s1"].get("retired")
    assert lb["strategies"]["s1"]["would_retire"] == "live results far below the backtest"
    E.run_portfolio(lb, [st], {"s1": {}}, {}, {}, 1.0, 2, freeze=False)
    assert "s1" not in lb["sleeves"] and lb["strategies"]["s1"]["retired"] == 2
