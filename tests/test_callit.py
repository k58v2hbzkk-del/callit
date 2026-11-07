"""Offline tests: a whole season with fake CoinGecko / ESPN / Solana responses.

Run:  python -m pytest -q tests   (from the folder that contains the callit/ package)
"""

import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PKG = Path(__file__).resolve().parents[1].name

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def cl(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("CALLIT_DB", os.path.join(tmp, "t.db"))
    store = importlib.import_module(f"{PKG}.store")
    store.reset_for_tests()
    eng = importlib.import_module(f"{PKG}.engine")
    orc = importlib.import_module(f"{PKG}.oracles")
    orc._CACHE.clear()
    return eng, orc


class Feed:
    """Fake HTTP: price is mutable, ESPN serves one Arsenal v Chelsea fixture."""

    def __init__(self):
        self.price = 250.0
        self.history = []          # [(datetime, price)]
        self.match_state = "pre"
        self.score = (0, 0)
        self.tx = None

    def __call__(self, url, body=None):
        if "simple/price" in url:
            return {"solana": {"usd": self.price}}
        if "market_chart/range" in url:
            return {"prices": [[d.timestamp() * 1000, p] for d, p in self.history]}
        if "espn" in url:
            if "/eng.1/" not in url:
                return {"events": []}
            done = self.match_state == "post"
            return {"events": [{
                "id": "777", "date": "2026-11-08T16:30Z", "name": "Chelsea at Arsenal",
                "status": {"type": {"state": self.match_state, "completed": done, "name": "STATUS_FULL_TIME" if done else "STATUS_SCHEDULED", "detail": "FT" if done else ""}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": str(self.score[0]), "team": {"displayName": "Arsenal", "shortDisplayName": "Arsenal", "abbreviation": "ARS"}},
                    {"homeAway": "away", "score": str(self.score[1]), "team": {"displayName": "Chelsea", "shortDisplayName": "Chelsea", "abbreviation": "CHE"}},
                ]}]}]}
        if body and body.get("method") == "getTransaction":
            return {"result": self.tx}
        raise AssertionError("unexpected url " + url)


def A(name):
    from importlib import import_module
    return import_module(f"{PKG}.engine").Actor(f"tg:{name.lower()}", name)


def test_price_call_close_mode_and_scoring(cl):
    eng, _ = cl
    f = Feed()
    r = eng.make_call("tg:g1", A("Taj"), "SOL above 300 by Friday 6pm", now=T0, fetch=f)
    assert r["ok"], r
    cid = r["call_id"]
    assert eng.join("tg:g1", cid, A("Truth"), "against", now=T0 + timedelta(minutes=5))["ok"]
    assert eng.join("tg:g1", cid, A("Ada"), "against", now=T0 + timedelta(minutes=6))["ok"]
    # no switching, no fading yourself
    assert not eng.join("tg:g1", cid, A("Ada"), "with", now=T0 + timedelta(minutes=7))["ok"]
    assert not eng.join("tg:g1", cid, A("Taj"), "against", now=T0 + timedelta(minutes=7))["ok"]
    # sides close after 25% of the window
    late = T0 + timedelta(days=2)
    assert not eng.join("tg:g1", cid, A("Bayo"), "with", now=late)["ok"]
    # before the deadline nothing settles (only the lock message)
    msgs = eng.referee("tg:g1", now=late, fetch=f)
    assert any("locked" in m for m in msgs)
    deadline = eng.dt(eng.get_call(cid)["deadline"])
    f.history = [(deadline - timedelta(minutes=30), 290.0), (deadline, 281.4)]
    msgs = eng.referee("tg:g1", now=deadline + timedelta(hours=1), fetch=f)
    assert len(msgs) == 1 and "WRONG" in msgs[0] and "281.4" in msgs[0]
    table = eng.view_table("tg:g1")
    assert table.index("Truth") < table.index("Taj")
    assert eng.get_call(cid)["status"] == "miss"


def test_touch_settles_early(cl):
    eng, _ = cl
    f = Feed()
    r = eng.make_call("tg:g1", A("Taj"), "SOL hits 300 by end of month", now=T0, fetch=f)
    cid = r["call_id"]
    assert eng.get_call(cid)["spec"]["op"] == "above"
    eng.join("tg:g1", cid, A("Truth"), "fade", now=T0)
    f.price = 301.5
    importlib.import_module(f"{PKG}.oracles")._CACHE.clear()
    msgs = eng.referee("tg:g1", now=T0 + timedelta(days=3), fetch=f)
    assert "CALLED IT" in msgs[-1]


def test_rejects_already_true(cl):
    eng, _ = cl
    f = Feed()
    f.price = 320
    r = eng.make_call("tg:g1", A("Taj"), "SOL hits 300 by Friday", now=T0, fetch=f)
    assert r["ok"]  # 'hits' flips to 'below 300' when price is already above
    assert eng.get_call(r["call_id"])["spec"]["op"] == "below"
    r = eng.make_call("tg:g1", A("Taj"), "SOL pumps to 300 by Friday", now=T0, fetch=f)
    assert not r["ok"] and "already" in r["message"]


def test_match_call_and_money(cl):
    eng, _ = cl
    f = Feed()
    r = eng.make_call("tg:g1", A("Taj"), "Arsenal beat Chelsea stake 5 USDC", now=T0, fetch=f)
    assert r["ok"], r
    cid = r["call_id"]
    c = eng.get_call(cid)
    assert c["kind"] == "match" and c["spec"]["pick"] == "home"
    assert c["lock_at"] == "2026-11-08T16:30:00+00:00"   # closes at kickoff
    eng.join("tg:g1", cid, A("Truth"), "against", now=T0)
    eng.join("tg:g1", cid, A("Ada"), "against", now=T0)
    eng.join("tg:g1", cid, A("Bayo"), "with", now=T0)
    # game still going: nothing settles
    f.match_state = "in"
    assert all("CALLIT #" not in m or "locked" in m for m in eng.referee("tg:g1", now=eng.dt(c["deadline"]), fetch=f))
    f.match_state, f.score = "post", (2, 1)
    importlib.import_module(f"{PKG}.oracles")._CACHE.clear()
    msgs = eng.referee("tg:g1", now=eng.dt(c["deadline"]) + timedelta(minutes=30), fetch=f)
    assert "CALLED IT" in msgs[-1] and "2-1" in msgs[-1]
    ledger = eng.view_debts("tg:g1")
    # 2 losers x 5 = 10 split over 2 winners -> two transfers of 5
    assert ledger.count("5 USDC") == 2


def test_onchain_receipt(cl):
    eng, orc = cl
    f = Feed()
    r = eng.make_call("tg:g1", A("Taj"), "SOL above 300 by tomorrow stake 1 USDC", now=T0, fetch=f)
    cid = r["call_id"]
    eng.join("tg:g1", cid, A("Truth"), "against", now=T0)
    d = eng.dt(eng.get_call(cid)["deadline"])
    f.history = [(d, 200.0)]
    eng.referee("tg:g1", now=d + timedelta(hours=1), fetch=f)
    debt = eng.store.db().execute("SELECT * FROM debts").fetchone()
    assert debt["from_name"] == "Taj" and debt["to_name"] == "Truth"
    taj_w = "7" * 43 + "A"
    truth_w = "8" * 43 + "B"
    eng.set_wallet(A("Taj"), taj_w)
    eng.set_wallet(A("Truth"), truth_w)
    sig = "5" * 87 + "x"
    f.tx = {"meta": {"err": None,
                     "preTokenBalances": [{"mint": orc.USDC_MINT, "owner": taj_w, "uiTokenAmount": {"uiAmount": 10}},
                                          {"mint": orc.USDC_MINT, "owner": truth_w, "uiTokenAmount": {"uiAmount": 0}}],
                     "postTokenBalances": [{"mint": orc.USDC_MINT, "owner": taj_w, "uiTokenAmount": {"uiAmount": 9}},
                                           {"mint": orc.USDC_MINT, "owner": truth_w, "uiTokenAmount": {"uiAmount": 1}}]},
            "transaction": {"message": {"accountKeys": []}}}
    assert not eng.mark_paid("tg:g1", debt["id"], A("Truth"), sig, fetch=f)["ok"]   # only the debtor posts receipts
    res = eng.mark_paid("tg:g1", debt["id"], A("Taj"), sig, fetch=f)
    assert res["ok"], res
    assert not eng.mark_paid("tg:g1", debt["id"], A("Taj"), sig, fetch=f)["ok"]      # no double use


def test_custom_vote_and_judge(cl):
    eng, _ = cl
    r = eng.make_call("tg:g1", A("Taj"), "Davido drops an album by Friday", now=T0)
    cid = r["call_id"]
    eng.join("tg:g1", cid, A("Truth"), "against", now=T0)
    d = eng.dt(eng.get_call(cid)["deadline"])
    assert not eng.vote("tg:g1", cid, A("Ada"), "yes", now=d - timedelta(hours=1))["ok"]
    assert "Time's up" in eng.referee("tg:g1", now=d + timedelta(minutes=1))[-1]
    eng.vote("tg:g1", cid, A("Taj"), "yes", now=d + timedelta(minutes=2))
    eng.vote("tg:g1", cid, A("Truth"), "no", now=d + timedelta(minutes=3))
    res = eng.vote("tg:g1", cid, A("Ada"), "yes", now=d + timedelta(minutes=4))   # neutral, counts double
    assert "CALLED IT" in res["message"]

    r = eng.make_call("tg:g1", A("Taj"), "Wizkid trends on X by tomorrow judge @Bayo", now=T0)
    cid = r["call_id"]
    d = eng.dt(eng.get_call(cid)["deadline"])
    res = eng.vote("tg:g1", cid, A("Bayo"), "no", now=d + timedelta(minutes=1))
    assert "WRONG" in res["message"] and "Judge" in res["message"]


def test_leagues_are_isolated(cl):
    eng, _ = cl
    r = eng.make_call("tg:g1", A("Taj"), "rain in Ilorin by tomorrow", now=T0)
    assert not eng.join("tg:g2", r["call_id"], A("Truth"), "against", now=T0)["ok"]


def test_netting():
    eng = importlib.import_module(f"{PKG}.engine")
    t = eng.net_transfers({"a": -10, "b": -5, "c": 9, "d": 6})
    assert round(sum(x[2] for x in t), 6) == 15 and len(t) <= 3


def test_command_router(cl, monkeypatch):
    eng, _ = cl
    plugin = importlib.import_module(PKG)
    meta = {"platform": "telegram", "chat_id": "-100", "thread_id": "", "name": "Squad"}
    out = plugin.run_command("rain in Ilorin by tomorrow", "telegram:-100", A("Taj"), meta)
    assert "CALLIT #" in out
    assert "AGAINST" in plugin.run_command("against 1", "telegram:-100", A("Truth"), meta)
    assert "Open calls" in plugin.run_command("board", "telegram:-100", A("Truth"), meta)
    assert "CALLIT" in plugin.run_command("help", "telegram:-100", A("Truth"), meta)
    # hotseat only works locally
    out = plugin.run_command("as Ada with 1", "local", A("You"), {"platform": "local"})
    assert "no call #1" in out.lower()


def test_duel_game_bet_concede_and_money(cl):
    eng, _ = cl
    r = eng.make_call("tg:g1", A("Taj"), "I beat you in EA FC tonight stake 20 USDC", now=T0, mode="duel", opponent="@truth")
    assert r["ok"], r
    cid = r["call_id"]
    assert "DUEL" in r["message"] and "@truth" in r["message"]
    # strangers can't join, the opponent joins by accepting
    assert not eng.join("tg:g1", cid, A("Ada"), "against", now=T0)["ok"]
    assert not eng.accept("tg:g1", cid, A("Ada"), now=T0)["ok"]
    assert "DUEL ON" in eng.accept("tg:g1", cid, A("Truth"), now=T0 + timedelta(minutes=5))["message"]
    # Truth concedes -> Taj wins, ₦2000 debt
    res = eng.report("tg:g1", cid, A("Truth"), "lost", now=T0 + timedelta(hours=3))
    assert "CALLED IT" in res["message"] and "20 USDC" in res["message"]


def test_duel_dispute_goes_to_vote(cl):
    eng, _ = cl
    cid = eng.make_call("tg:g1", A("Taj"), "I win at COD by tonight", now=T0, mode="duel", opponent="truth")["call_id"]
    eng.accept("tg:g1", cid, A("Truth"), now=T0)
    eng.report("tg:g1", cid, A("Taj"), "won", now=T0 + timedelta(hours=2))
    res = eng.report("tg:g1", cid, A("Truth"), "won", now=T0 + timedelta(hours=2))
    assert "Both sides claim" in res["message"]
    eng.vote("tg:g1", cid, A("Ada"), "no", now=T0 + timedelta(hours=2))   # neutral: counts 2
    res = eng.vote("tg:g1", cid, A("Bayo"), "no", now=T0 + timedelta(hours=2))
    assert "WRONG" in res["message"]


def test_duel_chicken(cl):
    eng, _ = cl
    cid = eng.make_call("tg:g1", A("Taj"), "Burna drops a single by Friday", now=T0, mode="duel", opponent="truth")["call_id"]
    lock = eng.dt(eng.get_call(cid)["lock_at"])
    msgs = eng.referee("tg:g1", now=lock + timedelta(minutes=1))
    assert "🐔" in msgs[0] and eng.get_call(cid)["status"] == "void"
    assert "Ran from a duel" in eng.view_shame("tg:g1")


def test_price_pool(cl):
    eng, orc = cl
    f = Feed()
    r = eng.make_pool("tg:g1", A("Taj"), "SOL price by tomorrow 6pm stake 1 USDC", now=T0, fetch=f)
    assert r["ok"], r
    cid = r["call_id"]
    for who, val in [("Taj", "260"), ("Truth", "300"), ("Ada", "255"), ("Bayo", "240")]:
        assert eng.guess("tg:g1", cid, A(who), val, now=T0 + timedelta(minutes=1))["ok"]
    assert not eng.guess("tg:g1", cid, A("Taj"), "999", now=T0)["ok"]          # one guess each
    assert not eng.guess("tg:g1", cid, A("Kemi"), "255", now=T0)["ok"]         # no duplicate answers
    d = eng.dt(eng.get_call(cid)["deadline"])
    f.history = [(d, 257.0)]
    orc._CACHE.clear()
    out = "\n".join(eng.referee("tg:g1", now=d + timedelta(hours=1), fetch=f))
    assert out.index("Ada") < out.index("Taj") < out.index("Bayo")             # 255, 260, 240
    assert "Truth" not in out.split("💀")[0] or "🥇 Ada" in out
    assert "USDC" in out                                                       # pot was split


def test_score_pool_and_custom_pool(cl):
    eng, orc = cl
    f = Feed()
    cid = eng.make_pool("tg:g1", A("Taj"), "Arsenal vs Chelsea score", now=T0, fetch=f)["call_id"]
    eng.guess("tg:g1", cid, A("Taj"), "2-1", now=T0)
    eng.guess("tg:g1", cid, A("Truth"), "0-0", now=T0)
    assert not eng.guess("tg:g1", cid, A("Ada"), "two", now=T0)["ok"]
    f.match_state, f.score = "post", (3, 1)
    orc._CACHE.clear()
    out = eng.referee("tg:g1", now=eng.dt(eng.get_call(cid)["deadline"]) + timedelta(minutes=5), fetch=f)[-1]
    assert "🥇 Taj" in out

    cid = eng.make_pool("tg:g1", A("Taj"), "how many goals Osimhen scores this month", now=T0)["call_id"]
    assert not eng.guess("tg:g1", cid, A("Taj"), "4", now=T0)["ok"]            # host judges, can't guess
    eng.guess("tg:g1", cid, A("Truth"), "3", now=T0)
    eng.guess("tg:g1", cid, A("Ada"), "6", now=T0)
    lock = eng.dt(eng.get_call(cid)["lock_at"])
    assert not eng.pool_result("tg:g1", cid, A("Truth"), "5", now=lock)["ok"]  # only the judge
    res = eng.pool_result("tg:g1", cid, A("Taj"), "5", now=lock + timedelta(minutes=1))
    assert "🥇 Ada" in res["message"]


def test_any_bet_defaults_deadline(cl):
    eng, _ = cl
    r = eng.make_call("tg:g1", A("Taj"), "NEPA brings light before 8pm", now=T0)
    assert r["ok"], r
    r = eng.make_call("tg:g1", A("Taj"), "Truth will be late to class", now=T0)
    assert r["ok"] and "24 hours" in r["message"]


class FakeChain:
    """Stands in for solana.Escrow: holds bet states, records payouts, can fail on demand."""
    cluster, program = "devnet", "9fhzt61u3YzUWYRY66E3ArvJVS2We8jvaHVuSLuUdT9Q"

    def __init__(self):
        self.bets, self.paid, self.fail = {}, [], False

    def get_bet(self, bet_id):
        return self.bets.get(bet_id)

    def settle(self, bet_id, outcome):
        if self.fail:
            raise RuntimeError("rpc down")
        self.paid.append((bet_id, outcome))
        self.bets[bet_id] = {**self.bets[bet_id], "status": "done"}
        return f"sig{len(self.paid)}"

    def explorer(self, sig):
        return f"https://solscan.io/tx/{sig}?cluster=devnet"


def _settings(chain):
    eng = importlib.import_module(f"{PKG}.engine")
    return eng.Settings(escrow=chain, bot_username="CallitHQBot")


def test_fiat_stakes_rejected(cl):
    eng, _ = cl
    r = eng.make_call("tg:g1", A("Taj"), "Arsenal win the league by May stake ₦2000", now=T0)
    assert not r["ok"] and "crypto only" in r["message"]
    r = eng.make_call("tg:g1", A("Taj"), "rain tomorrow stake $5", now=T0)
    assert not r["ok"]


def test_escrow_duel_full_flow(cl):
    eng, _ = cl
    ch = FakeChain()
    S = _settings(ch)
    r = eng.make_call("tg:g1", A("Taj"), "I beat you in EA FC tonight stake 0.1 SOL", settings=S, now=T0, mode="duel", opponent="truth")
    assert r["ok"] and "escrow" in r["message"] and "a=create" in r["message"] and "k58v2hbzkk-del.github.io" in r["message"]
    cid = r["call_id"]
    bid = eng.get_call(cid)["spec"]["escrow"]["bet_id"]
    # nothing happens until the challenger deposits
    assert eng.referee("tg:g1", settings=S, now=T0 + timedelta(minutes=1)) == []
    ch.bets[bid] = {"status": "open", "creator": "WalletTaj", "opponent": "1" * 32}
    assert "stake is locked" in eng.referee("tg:g1", settings=S, now=T0 + timedelta(minutes=2))[0]
    # can't report before both stakes are in
    acc = eng.accept("tg:g1", cid, A("Truth"), settings=S, now=T0 + timedelta(minutes=3))
    assert "a=join" in acc["message"]
    assert not eng.report("tg:g1", cid, A("Truth"), "lost", settings=S, now=T0 + timedelta(minutes=4))["ok"]
    ch.bets[bid] = {"status": "active", "creator": "WalletTaj", "opponent": "WalletTruth"}
    assert "IS LIVE" in "\n".join(eng.referee("tg:g1", settings=S, now=T0 + timedelta(minutes=5)))
    res = eng.report("tg:g1", cid, A("Truth"), "lost", settings=S, now=T0 + timedelta(hours=2))
    assert "Paid 0.2 SOL to Taj on-chain" in res["message"] and ch.paid == [(bid, 0)]
    assert "owes" not in eng.view_debts("tg:g1") and "#1:" not in res["message"]   # no IOUs for escrowed duels


def test_escrow_payout_retries(cl):
    eng, _ = cl
    ch = FakeChain()
    S = _settings(ch)
    cid = eng.make_call("tg:g1", A("Taj"), "I win at COD by tonight stake 5 USDC", settings=S, now=T0, mode="duel", opponent="truth")["call_id"]
    bid = eng.get_call(cid)["spec"]["escrow"]["bet_id"]
    ch.bets[bid] = {"status": "open", "creator": "W1", "opponent": "1" * 32}
    eng.referee("tg:g1", settings=S, now=T0)
    eng.accept("tg:g1", cid, A("Truth"), settings=S, now=T0)
    ch.bets[bid] = {"status": "active", "creator": "W1", "opponent": "W2"}
    eng.referee("tg:g1", settings=S, now=T0)
    ch.fail = True
    res = eng.report("tg:g1", cid, A("Taj"), "lost", settings=S, now=T0 + timedelta(hours=1))
    assert "queued" in res["message"]
    ch.fail = False
    out = eng.referee("tg:g1", settings=S, now=T0 + timedelta(hours=1, minutes=15))
    assert any("went through" in m for m in out) and ch.paid == [(bid, 1)]


def test_escrow_refunds(cl):
    eng, _ = cl
    ch = FakeChain()
    S = _settings(ch)
    # challenger locks, opponent never accepts -> refund at lock time
    cid = eng.make_call("tg:g1", A("Taj"), "Burna drops a single by Friday stake 0.1 SOL", settings=S, now=T0, mode="duel", opponent="truth")["call_id"]
    bid = eng.get_call(cid)["spec"]["escrow"]["bet_id"]
    ch.bets[bid] = {"status": "open", "creator": "W1", "opponent": "1" * 32}
    eng.referee("tg:g1", settings=S, now=T0)
    lock = eng.dt(eng.get_call(cid)["lock_at"])
    out = "\n".join(eng.referee("tg:g1", settings=S, now=lock + timedelta(minutes=1)))
    assert "refunded on-chain" in out and ch.paid == [(bid, 2)]
    # nobody deposited at all -> void, nothing to pay
    cid = eng.make_call("tg:g1", A("Taj"), "rain tomorrow stake 0.1 SOL", settings=S, now=T0, mode="duel", opponent="truth")["call_id"]
    lock = eng.dt(eng.get_call(cid)["lock_at"])
    out = "\n".join(eng.referee("tg:g1", settings=S, now=lock + timedelta(minutes=1)))
    assert "void" in out and len(ch.paid) == 1
