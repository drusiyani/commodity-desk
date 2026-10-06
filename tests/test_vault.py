"""Encryption of the private data: round trips, wrong passwords, tampering, and the browser's exact format."""
import base64
import json

import pytest

import vault as V


def test_round_trip_and_format():
    blob = V.encrypt({"trades": [1, 2, 3]}, "correct horse", iters=1000)
    assert V.is_encrypted(blob) and blob["kdf"] == "PBKDF2-SHA256" and blob["iter"] == 1000
    assert len(base64.b64decode(blob["iv"])) == 12 and len(base64.b64decode(blob["salt"])) == 16
    assert "trades" not in json.dumps(blob)                       # nothing readable left
    assert V.decrypt(blob, "correct horse") == {"trades": [1, 2, 3]}


def test_wrong_password_and_tampering_are_refused():
    blob = V.encrypt({"a": 1}, "pw", iters=1000)
    with pytest.raises(V.Locked):
        V.decrypt(blob, "not it")
    ct = bytearray(base64.b64decode(blob["ct"]))
    ct[0] ^= 1
    with pytest.raises(V.Locked):
        V.decrypt(dict(blob, ct=base64.b64encode(bytes(ct)).decode()), "pw")


def test_every_file_gets_its_own_iv():
    a, b = V.encrypt({"x": 1}, "pw", iters=1000), V.encrypt({"x": 1}, "pw", iters=1000)
    assert a["iv"] != b["iv"] and a["ct"] != b["ct"]


def test_known_answer_matches_the_standard():
    # PBKDF2-HMAC-SHA256 test vector (RFC 7914 section 11), so the browser's Web Crypto derives the same key
    key = V.derive("passwd", b"salt", 1)
    assert key.hex()[:32] == "55ac046e56e3089fec1691c22544b605"


def test_missing_password(monkeypatch):
    monkeypatch.delenv("ARGON_PASSWORD", raising=False)
    with pytest.raises(V.Locked, match="ARGON_PASSWORD"):
        V.password()


# ---------- the engine's storage: private files encrypted at rest, migrated from the old plain copies ----------
import engine as E  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "DATA", tmp_path / "site" / "data")
    monkeypatch.setattr(E, "STATE", tmp_path / "state")
    monkeypatch.setenv("ARGON_PASSWORD", "test password")
    monkeypatch.setattr(V, "ITER", 1000)
    monkeypatch.setattr(V.encrypt, "__defaults__", (None, 1000))
    E.DATA.mkdir(parents=True)
    return tmp_path


def test_private_files_are_encrypted_and_the_plain_copy_removed(dirs):
    (E.DATA / "trades.json").write_text(json.dumps([{"symbol": "GC=F", "pnl": 12.5}]))   # an old plain copy
    trades = E.load("trades.json", [])
    assert trades[0]["pnl"] == 12.5                                     # migration: read the old copy once
    E.save("trades.json", trades)
    assert not (E.DATA / "trades.json").exists()
    raw = (E.STATE / "trades.json.enc").read_text()
    assert "GC=F" not in raw and V.is_encrypted(json.loads(raw))
    assert E.load("trades.json", []) == trades                         # and read back through the vault
    E.save("prices.json", {"x": 1})                                     # public files stay plain
    assert json.loads((E.DATA / "prices.json").read_text()) == {"x": 1}


def test_wrong_or_missing_password_stops_reading(dirs, monkeypatch):
    E.save("portfolio.json", {"cash": 1})
    monkeypatch.setenv("ARGON_PASSWORD", "wrong")
    with pytest.raises(V.Locked):
        E.load("portfolio.json", None)
    monkeypatch.delenv("ARGON_PASSWORD")
    with pytest.raises(V.Locked):
        E.save("portfolio.json", {"cash": 2})


def test_the_salt_is_kept_so_the_site_key_stays_the_same(dirs):
    s1 = E.salt()
    E.save("calls.json", [])
    assert E.salt() == s1 and json.loads((E.STATE / "calls.json.enc").read_text())["salt"] == \
        base64.b64encode(s1).decode()


def test_site_bundle_holds_the_private_parts(dirs):
    E.save_site_bundle({"trades": [1], "decisions": [{"thinking": "secret"}]})
    raw = (E.DATA / E.SITE_PRIVATE).read_text()
    assert "secret" not in raw
    got = V.decrypt(json.loads(raw), "test password")
    assert got["decisions"][0]["thinking"] == "secret" and "t" in got


def test_news_comments_are_split_from_the_headlines():
    news = [{"id": "n1", "title": "Gold up", "claude": {"impact": "bullish", "take": "secret"}}, {"id": "n2", "title": "Oil"}]
    public, takes = E.split_news(news)
    assert "claude" not in json.dumps(public) and takes == {"n1": {"impact": "bullish", "take": "secret"}}
    assert E.join_news(public, takes)[0]["claude"]["take"] == "secret"


def test_public_summary_has_no_individual_trades():
    eq = [{"time": 1, "equity": 100_000, "benchmark": 100_000, "lab": 100_000, "ict": 100_000, "kronos": 100_000, "rules": 100_000},
          {"time": 2, "equity": 101_000, "benchmark": 99_000, "lab": 100_000, "ict": 100_000, "kronos": 100_000, "rules": 100_000}]
    trades = [{"action": "BUY", "symbol": "GC=F", "source": "claude"}, {"action": "SELL", "pnl": 50.0, "symbol": "GC=F", "source": "claude"},
              {"action": "BUY", "symbol": "CL=F", "source": "core"}]
    calls = [{"symbol": "GC=F", "bias": "bullish", "score": 70, "checked": True, "right": True}, {"symbol": "CL=F", "bias": "bearish", "checked": False}]
    lb = {"strategies": {"s1": {"name": "A", "r": [0.5, -1], "equity": 50_000}}}
    s = E.public_summary(3, eq, trades, [], [], [], [], calls, lb, {"positions": {}}, [])
    assert s["perf"]["equity"] == {"ret": 0.01, "buys": 1, "closed": 1, "win": 1.0, "realised": 50.0, "dd": 0.0}
    assert s["perf"]["benchmark"]["dd"] == pytest.approx(-0.01)
    assert s["report"]["all"] == [1, 1] and s["report"]["pending"] == 1 and s["report"]["groups"]["Metals"] == [1, 1]
    assert s["lab_live"]["s1"] == {"name": "A", "equity": 50_000, "trades": 2}
    assert "GC=F" not in json.dumps(s)
