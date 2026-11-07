"""CALLIT league engine: calls, sides, locking, refereeing, scoring, stakes and receipts.

Everything here is deterministic given (database, clock, oracle fetcher), which is what lets
the referee run unattended and lets tests replay whole seasons offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import oracles, store
from .parse import ONCHAIN, ParsedCall, Stake, parse_call

MAX_OPEN_CALLS_PER_PLAYER = 10
MATCH_RESULT_DELAY = timedelta(minutes=110)   # kickoff + 110' before we ask for a final score
VOID_AFTER = timedelta(hours=72)              # data never arrived -> refund, no points
VOTE_WINDOW = timedelta(hours=48)


@dataclass
class Actor:
    user_id: str
    name: str


@dataclass
class Settings:
    tz: str = "UTC"
    min_votes: int = 3
    rpc_url: str = "https://api.mainnet-beta.solana.com"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def fmt_time(d: datetime, tz: str) -> str:
    try:
        local = d.astimezone(ZoneInfo(tz))
    except Exception:
        local = d
    return local.strftime("%a %d %b, %H:%M ") + (local.tzname() or "UTC")


def money(amount: float, ccy: str) -> str:
    return Stake(round(amount, 6), ccy).label()


def _log(conn, league: str, call_id: Optional[int], event: str, detail: dict, at: Optional[datetime] = None) -> None:
    conn.execute("INSERT INTO log(league, call_id, at, event, detail) VALUES (?,?,?,?,?)",
                 (league, call_id, iso(at or utcnow()), event, json.dumps(detail, default=str)))


# ====================================================================== leagues

def ensure_league(key: str, platform: str = "", chat_id: str = "", thread_id: str = "", name: str = "") -> dict:
    conn = store.db()
    with store.lock():
        row = conn.execute("SELECT * FROM leagues WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO leagues(key, platform, chat_id, thread_id, name, season, season_name, created_at) "
                         "VALUES (?,?,?,?,?,1,'Season 1',?)", (key, platform, chat_id, thread_id, name, iso(utcnow())))
            conn.commit()
            row = conn.execute("SELECT * FROM leagues WHERE key=?", (key,)).fetchone()
        elif name and row["name"] != name:
            conn.execute("UPDATE leagues SET name=? WHERE key=?", (name, key))
            conn.commit()
        return dict(row)


def new_season(key: str, label: str = "") -> str:
    conn = store.db()
    with store.lock():
        lg = ensure_league(key)
        nxt = lg["season"] + 1
        open_n = conn.execute("SELECT COUNT(*) FROM calls WHERE league=? AND status IN ('open','locked')", (key,)).fetchone()[0]
        champ = _table_rows(key, lg["season"])[:1]
        conn.execute("UPDATE leagues SET season=?, season_name=? WHERE key=?", (nxt, label or f"Season {nxt}", key))
        _log(conn, key, None, "season", {"from": lg["season"], "to": nxt})
        conn.commit()
    crown = f"👑 {lg['season_name']} champion: {champ[0]['name']} with {champ[0]['points']} pts\n" if champ else ""
    carry = f"\n({open_n} open call{'s' if open_n != 1 else ''} still count for the season they were made in.)" if open_n else ""
    return f"{crown}🆕 {label or f'Season {nxt}'} starts now. Everyone is back to zero.{carry}"


# ====================================================================== making calls

def _player(conn, league: str, season: int, actor: Actor) -> None:
    conn.execute("INSERT INTO players(league, season, user_id, name) VALUES (?,?,?,?) "
                 "ON CONFLICT(league, season, user_id) DO UPDATE SET name=excluded.name",
                 (league, season, actor.user_id, actor.name))


def lock_time(created: datetime, deadline: datetime, kickoff: Optional[datetime] = None) -> datetime:
    """Entries close at kickoff for matches; otherwise after 25% of the window (min 10 minutes).

    Late joiners can't wait until the answer is obvious, which is the main way prediction
    games get gamed among friends.
    """
    if kickoff:
        return min(kickoff, deadline)
    window = deadline - created
    return min(deadline, created + max(timedelta(minutes=10), window * 0.25))


def make_call(league: str, actor: Actor, raw: Optional[str] = None, *, parsed: Optional[ParsedCall] = None,
              settings: Settings = Settings(), now: Optional[datetime] = None,
              fetch: oracles.Fetch = oracles.http_fetch, mode: str = "call", opponent: Optional[str] = None) -> dict:
    now = now or utcnow()
    pc = parsed or parse_call(raw or "", now, settings.tz)
    if mode == "duel":
        opponent = (opponent or "").lstrip("@").strip()
        if not opponent:
            return {"ok": False, "message": "Who are you challenging? /callit duel @name <your call>"}
        if opponent.lower() in {actor.name.lower().lstrip("@"), actor.user_id.lower()}:
            return {"ok": False, "message": "You can't duel yourself 😂"}
    if pc.errors:
        return {"ok": False, "message": "Couldn't take that call: " + "; ".join(pc.errors) + "."}
    conn = store.db()
    lg = ensure_league(league)
    open_mine = conn.execute("SELECT COUNT(*) FROM calls WHERE league=? AND author_id=? AND status IN ('open','locked')",
                             (league, actor.user_id)).fetchone()[0]
    if open_mine >= MAX_OPEN_CALLS_PER_PLAYER:
        return {"ok": False, "message": f"You already have {open_mine} open calls. Let some settle first 😅"}

    spec = dict(pc.spec)
    deadline = pc.deadline
    kickoff = None
    note = ""

    if pc.kind == "price":
        try:
            coin = oracles.resolve_coin(spec["symbol"], fetch)
        except Exception:
            coin = None
        if not coin:
            return {"ok": False, "message": f"I can't find a coin called {spec['symbol']} on CoinGecko."}
        try:
            live = oracles.price_now(coin["id"], fetch)
        except Exception:
            live = None
        if live is None:
            return {"ok": False, "message": "Price feed is down right now, try again in a minute."}
        spec.update(coin_id=coin["id"], start_price=live)
        if spec["op"] == "auto":
            spec["op"] = "above" if spec["target"] > live else "below"
        if spec["mode"] == "touch" and ((spec["op"] == "above" and live >= spec["target"])
                                         or (spec["op"] == "below" and live <= spec["target"])):
            return {"ok": False, "message": f"{spec['symbol']} is already at ${live:,.4g}. That's not a call, that's a fact 😂"}
        deadline = deadline or now + timedelta(hours=24)
        note = f"{spec['symbol']} is ${live:,.6g} right now."
    elif pc.kind == "match":
        try:
            fx = oracles.find_fixture(spec["team_a"], spec["team_b"], now, fetch=fetch)
        except Exception:
            fx = None
        if not fx:
            if not deadline:
                return {"ok": False, "message": (f"I couldn't find an upcoming {spec['team_a']} vs {spec['team_b']} fixture in the "
                                                 "next 14 days. Add a deadline and a judge to run it as a custom call, e.g. "
                                                 "'… by Sunday judge @name'.")}
            pc.kind, spec = "custom", {}
        else:
            kickoff = dt(fx["kickoff"])
            if kickoff <= now:
                return {"ok": False, "message": f"{fx['name']} has already kicked off. Calls close at kickoff."}
            pick = spec["pick"]
            if pick == "a":
                pick = "home" if fx["a_is_home"] else "away"
            spec = {**fx, "pick": pick, "pick_team": fx["home"] if pick == "home" else fx["away"] if pick == "away" else "Draw"}
            deadline = kickoff + MATCH_RESULT_DELAY
            note = f"{fx['name']} kicks off {fmt_time(kickoff, settings.tz)}."
    if pc.kind == "custom":
        if not deadline:
            deadline = now + timedelta(hours=24)
            note = "No deadline given, so it runs for 24 hours."
        spec["judge"] = pc.judge
    spec["game"] = mode
    if mode == "duel":
        spec["opponent"] = opponent

    if deadline <= now:
        return {"ok": False, "message": "That deadline has already passed."}
    if deadline - now > timedelta(days=366):
        return {"ok": False, "message": "Keep deadlines within a year."}
    lock_at = lock_time(now, deadline, kickoff)
    if mode == "duel" and not kickoff:  # give the challenged friend time to see it and accept
        lock_at = min(deadline, now + max(timedelta(minutes=30), (deadline - now) * 0.5))

    with store.lock():
        cur = conn.execute(
            "INSERT INTO calls(league, season, author_id, author_name, text, kind, spec, created_at, lock_at, deadline, "
            "stake_amount, stake_ccy, judge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (league, lg["season"], actor.user_id, actor.name, pc.text, pc.kind, json.dumps(spec), iso(now), iso(lock_at),
             iso(deadline), pc.stake.amount if pc.stake else None, pc.stake.currency if pc.stake else None, pc.judge))
        cid = cur.lastrowid
        conn.execute("INSERT INTO entries(call_id, user_id, user_name, side, joined_at) VALUES (?,?,?,?,?)",
                     (cid, actor.user_id, actor.name, "with", iso(now)))
        _player(conn, league, lg["season"], actor)
        conn.execute("UPDATE players SET calls_made=calls_made+1 WHERE league=? AND season=? AND user_id=?",
                     (league, lg["season"], actor.user_id))
        _log(conn, league, cid, "call", {"by": actor.name, "text": pc.text, "kind": pc.kind, "spec": spec}, now)
        conn.commit()

    call = get_call(cid)
    if mode == "duel":
        return {"ok": True, "call_id": cid, "call": call, "message": _duel_card(call, note, settings)}
    stake_txt = f"\n💰 Stake: {money(call['stake_amount'], call['stake_ccy'])} each side" if call["stake_amount"] else ""
    how = {
        "price": "I'll check the live price myself and settle it.",
        "match": "I'll check the final score myself and settle it.",
        "custom": (f"Judge: {pc.judge}. They decide when time's up." if pc.judge else
                   f"When time's up the group votes (/callit vote {cid} yes|no)."),
    }[call["kind"]]
    msg = (f"📣 CALLIT #{cid} by {actor.name}\n“{call['text']}”\n"
           f"{(note + chr(10)) if note else ''}"
           f"⏳ Deadline: {fmt_time(deadline, settings.tz)}\n"
           f"🔒 Sides close: {fmt_time(lock_at, settings.tz)}{stake_txt}\n"
           f"⚖️ {how}\n\n"
           f"Agree? /callit with {cid}   ·   Think it's cap? /callit against {cid}")
    return {"ok": True, "call_id": cid, "message": msg, "call": call}


def get_call(cid: int) -> Optional[dict]:
    row = store.db().execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["spec"] = json.loads(d["spec"] or "{}")
    return d


def entries(cid: int) -> list[dict]:
    return [dict(r) for r in store.db().execute("SELECT * FROM entries WHERE call_id=? ORDER BY joined_at", (cid,))]


# ====================================================================== joining

def join(league: str, cid: int, actor: Actor, side: str, *, settings: Settings = Settings(),
         now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    side = {"with": "with", "back": "with", "for": "with", "yes": "with", "agree": "with",
            "against": "against", "fade": "against", "no": "against", "doubt": "against", "cap": "against"}.get(side.lower())
    if not side:
        return {"ok": False, "message": "Pick a side: with or against."}
    call = get_call(cid)
    if not call or call["league"] != league:
        return {"ok": False, "message": f"There's no call #{cid} in this chat."}
    game = call["spec"].get("game", "call")
    if game == "pool":
        return {"ok": False, "message": f"#{cid} is a pool. Drop your guess: /callit guess {cid} <your answer>"}
    if game == "duel":
        if side == "against" and is_opponent(call, actor):
            return accept(league, cid, actor, settings=settings, now=now)
        return {"ok": False, "message": f"⚔️ #{cid} is a private duel between {call['author_name']} and @{call['spec']['opponent']}. "
                                        f"Watch it: /callit show {cid}"}
    if call["status"] != "open" or now >= dt(call["lock_at"]):
        return {"ok": False, "message": f"Too late, sides on #{cid} closed {fmt_time(dt(call['lock_at']), settings.tz)} 🔒"}
    conn = store.db()
    with store.lock():
        mine = conn.execute("SELECT side FROM entries WHERE call_id=? AND user_id=?", (cid, actor.user_id)).fetchone()
        if mine:
            return {"ok": False, "message": f"You're already {mine['side']} #{cid}. No switching sides 😤"}
        if side == "against" and actor.user_id == call["author_id"]:
            return {"ok": False, "message": "You can't fade your own call 😂"}
        conn.execute("INSERT INTO entries(call_id, user_id, user_name, side, joined_at) VALUES (?,?,?,?,?)",
                     (cid, actor.user_id, actor.name, side, iso(now)))
        _player(conn, league, call["season"], actor)
        _log(conn, league, cid, "join", {"by": actor.name, "side": side}, now)
        conn.commit()
    ents = entries(cid)
    w = sum(e["side"] == "with" for e in ents)
    a = len(ents) - w
    stake = f" ({money(call['stake_amount'], call['stake_ccy'])} in)" if call["stake_amount"] else ""
    emoji = "🤝" if side == "with" else "🙅"
    return {"ok": True, "message": f"{emoji} {actor.name} is {side.upper()} #{cid}{stake}. Now {w} with · {a} against."}


# ====================================================================== voting (custom calls)

def vote(league: str, cid: int, actor: Actor, verdict: str, *, settings: Settings = Settings(),
         now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    v = {"yes": "yes", "y": "yes", "true": "yes", "happened": "yes", "right": "yes",
         "no": "no", "n": "no", "false": "no", "didnt": "no", "wrong": "no"}.get(verdict.lower().replace("'", ""))
    if not v:
        return {"ok": False, "message": "Vote yes (it happened) or no (it didn't)."}
    call = get_call(cid)
    if not call or call["league"] != league:
        return {"ok": False, "message": f"There's no call #{cid} in this chat."}
    if call["kind"] != "custom":
        return {"ok": False, "message": f"#{cid} is settled by live data, no vote needed 🤖"}
    if call["status"] not in ("open", "locked"):
        return {"ok": False, "message": f"#{cid} is already settled."}
    if now < dt(call["deadline"]) and not call["spec"].get("disputed"):
        return {"ok": False, "message": f"Voting on #{cid} opens at the deadline: {fmt_time(dt(call['deadline']), settings.tz)}."}
    conn = store.db()
    with store.lock():
        conn.execute("INSERT INTO votes(call_id, user_id, user_name, verdict, at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(call_id, user_id) DO UPDATE SET verdict=excluded.verdict, at=excluded.at",
                     (cid, actor.user_id, actor.name, v, iso(now)))
        _log(conn, league, cid, "vote", {"by": actor.name, "verdict": v}, now)
        conn.commit()
    res = referee(league, settings=settings, now=now, only=cid)
    if res:
        return {"ok": True, "message": "\n\n".join(res)}
    tally = _tally(cid, call)
    return {"ok": True, "message": f"🗳️ {actor.name} voted {v.upper()} on #{cid}. Score so far: {tally['yes']} yes · {tally['no']} no "
                                   f"(neutral votes count double; settles at {settings.min_votes} points)."}


def _tally(cid: int, call: dict) -> dict:
    sides = {e["user_id"]: e["side"] for e in entries(cid)}
    rows = store.db().execute("SELECT * FROM votes WHERE call_id=?", (cid,)).fetchall()
    out = {"yes": 0, "no": 0, "count": len(rows), "judge": None}
    judge = (call.get("judge") or "").lower().lstrip("@")
    for r in rows:
        weight = 1 if r["user_id"] in sides else 2   # people with skin in the game count once
        out[r["verdict"]] += weight
        if judge and judge in {str(r["user_name"] or "").lower().lstrip("@"), str(r["user_id"]).lower()}:
            out["judge"] = r["verdict"]
    return out


# ====================================================================== refereeing

def due_calls(league: Optional[str]) -> list[dict]:
    q = "SELECT id FROM calls WHERE status IN ('open','locked')" + (" AND league=?" if league else "")
    return [get_call(r["id"]) for r in store.db().execute(q, (league,) if league else ())]


def referee(league: Optional[str] = None, *, settings: Settings = Settings(), now: Optional[datetime] = None,
            fetch: oracles.Fetch = oracles.http_fetch, only: Optional[int] = None) -> list[str]:
    """Advance every due call. Returns chat announcements (empty list = nothing to say)."""
    now = now or utcnow()
    out: list[str] = []
    for call in due_calls(league):
        if only and call["id"] != only:
            continue
        try:
            msg = _referee_one(call, settings, now, fetch)
        except Exception as exc:  # never let one bad call stop the rest
            store.db().execute("INSERT INTO log(league, call_id, at, event, detail) VALUES (?,?,?,?,?)",
                               (call["league"], call["id"], iso(now), "referee_error", repr(exc)))
            store.db().commit()
            msg = None
        if msg:
            out.append(msg)
    return out


def _lock_if_due(call: dict, now: datetime, settings: Settings) -> Optional[str]:
    if call["status"] != "open" or now < dt(call["lock_at"]):
        return None
    game = call["spec"].get("game", "call")
    if game == "duel" and len(entries(call["id"])) < 2:
        opp = call["spec"]["opponent"]
        _log(store.db(), call["league"], call["id"], "chicken", {"who": opp}, now)
        msg = settle(call, None, f"@{opp} never accepted", settings=settings, now=now)
        return f"🐔 @{opp} didn't accept duel #{call['id']} in time.\n" + msg
    if game == "pool":
        n = store.db().execute("SELECT COUNT(*) FROM guesses WHERE call_id=?", (call["id"],)).fetchone()[0]
        store.db().execute("UPDATE calls SET status='locked' WHERE id=? AND status='open'", (call["id"],))
        store.db().commit()
        call["status"] = "locked"
        return f"🔒 Pool #{call['id']} is closed with {n} guess{'es' if n != 1 else ''}. “{call['text']}”"
    conn = store.db()
    with store.lock():
        conn.execute("UPDATE calls SET status='locked' WHERE id=? AND status='open'", (call["id"],))
        conn.commit()
    call["status"] = "locked"
    ents = entries(call["id"])
    w = [e["user_name"] for e in ents if e["side"] == "with"]
    a = [e["user_name"] for e in ents if e["side"] == "against"]
    if not a and not call["stake_amount"]:
        return None  # unopposed, no money: lock quietly
    return (f"🔒 #{call['id']} is locked. “{call['text']}”\n"
            f"🤝 With: {', '.join(w) or 'nobody'}\n🙅 Against: {', '.join(a) or 'nobody'}")


def _referee_one(call: dict, settings: Settings, now: datetime, fetch) -> Optional[str]:
    lock_msg = _lock_if_due(call, now, settings)
    if call["status"] not in ("open", "locked"):
        return lock_msg
    if call["spec"].get("game") == "pool":
        return "\n\n".join(x for x in (lock_msg, _referee_pool(call, settings, now, fetch)) if x) or None
    spec = call["spec"]
    deadline = dt(call["deadline"])
    result: Optional[tuple] = None   # (outcome True/False/None=void, note)

    if call["kind"] == "price":
        target, op = float(spec["target"]), spec["op"]
        hit = (lambda p: p >= target) if op == "above" else (lambda p: p <= target)
        if spec["mode"] == "touch":
            if now < deadline:
                live = oracles.price_now(spec["coin_id"], fetch)
                if live is not None and hit(live):
                    result = (True, f"{spec['symbol']} touched ${live:,.6g} before the deadline")
            else:
                pts = oracles.price_range(spec["coin_id"], dt(call["created_at"]), deadline, fetch)
                if pts:
                    extreme = max(p for _, p in pts) if op == "above" else min(p for _, p in pts)
                    result = (hit(extreme), f"{'High' if op == 'above' else 'Low'} before the deadline was ${extreme:,.6g}")
                elif now - deadline > VOID_AFTER:
                    result = (None, "no price data came back")
        elif now >= deadline:
            if now - deadline < timedelta(minutes=10):
                p = oracles.price_now(spec["coin_id"], fetch)
            else:
                pts = oracles.price_range(spec["coin_id"], deadline - timedelta(hours=2), deadline + timedelta(minutes=10), fetch)
                before = [x for x in pts if x[0] <= deadline + timedelta(minutes=5)]
                p = (before or pts)[-1][1] if pts else None
            if p is not None:
                result = (hit(p), f"{spec['symbol']} was ${p:,.6g} at the deadline")
            elif now - deadline > VOID_AFTER:
                result = (None, "no price data came back")

    elif call["kind"] == "match":
        if now >= deadline:
            res = oracles.match_result(spec, fetch)
            if res and res.get("final"):
                if res.get("void"):
                    result = (None, f"match {res.get('detail') or 'called off'}")
                else:
                    hs, as_ = res["home_score"], res["away_score"]
                    actual = "home" if hs > as_ else "away" if as_ > hs else "draw"
                    result = (actual == spec["pick"], f"Final: {res['home']} {hs}-{as_} {res['away']}")
            elif now - deadline > VOID_AFTER:
                result = (None, "no final score was published")

    else:  # custom
        if now >= deadline or spec.get("disputed"):
            t = _tally(call["id"], call)
            if call.get("judge") and t["judge"]:
                result = (t["judge"] == "yes", f"Judge {call['judge']} ruled {t['judge'].upper()}")
            elif not call.get("judge") and (t["yes"] + t["no"]) >= settings.min_votes and t["yes"] != t["no"]:
                result = (t["yes"] > t["no"], f"Group vote: {t['yes']} yes · {t['no']} no")
            elif now - deadline > VOTE_WINDOW:
                if t["yes"] != t["no"] and t["count"]:
                    result = (t["yes"] > t["no"], f"Vote closed: {t['yes']} yes · {t['no']} no")
                else:
                    result = (None, "no clear verdict after 48h")
            elif not call["announced"]:
                store.db().execute("UPDATE calls SET announced=1 WHERE id=?", (call["id"],))
                store.db().commit()
                who = f"{call['judge']}, it's your call" if call.get("judge") else "Everyone vote"
                msg = (f"⏰ Time's up on #{call['id']}: “{call['text']}”\n{who}: /callit vote {call['id']} yes|no"
                       + ("" if call.get("judge") else "\n(Neutral voters count double.)"))
                return "\n\n".join(x for x in (lock_msg, msg) if x)

    if result is None:
        return lock_msg
    settled = settle(call, result[0], result[1], settings=settings, now=now)
    return "\n\n".join(x for x in (lock_msg, settled) if x)


# ====================================================================== settling + scoring

def settle(call: dict, outcome: Optional[bool], note: str, *, settings: Settings = Settings(),
           now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    conn = store.db()
    ents = entries(call["id"])
    with store.lock():
        fresh = conn.execute("SELECT status FROM calls WHERE id=?", (call["id"],)).fetchone()
        if fresh["status"] not in ("open", "locked"):
            return ""
        status = "void" if outcome is None else ("hit" if outcome else "miss")
        conn.execute("UPDATE calls SET status=?, outcome=?, result_note=?, settled_at=? WHERE id=?",
                     (status, None if outcome is None else ("true" if outcome else "false"), note, iso(now), call["id"]))
        _log(conn, call["league"], call["id"], "settle", {"status": status, "note": note}, now)
        if outcome is None:
            conn.commit()
            return (f"🤷 CALLIT #{call['id']} is void: {note}.\n“{call['text']}”\n"
                    "No points, no money moves.")

        win_side = "with" if outcome else "against"
        winners = [e for e in ents if e["side"] == win_side]
        losers = [e for e in ents if e["side"] != win_side]
        lines = []
        gain = 10 + 5 * min(len(losers), 8)          # beating more people pays more
        for e in winners:
            p = _bump(conn, call, e, won=True, base=gain + (5 if e["user_id"] == call["author_id"] else 0))
            lines.append(f"{e['user_name']} +{p['delta']}" + (f" 🔥{p['streak']}" if p["streak"] >= 2 else ""))
        loss_lines = []
        for e in losers:
            p = _bump(conn, call, e, won=False, base=-5)
            loss_lines.append(f"{e['user_name']} {p['delta']:+d}")
        debt_lines = _make_debts(conn, call, winners, losers, now)
        conn.commit()

    verdict = "✅ CALLED IT" if outcome else "❌ WRONG"
    head = f"🏁 CALLIT #{call['id']} · {verdict}\n“{call['text']}” by {call['author_name']}\n📊 {note}" + ("." if note[-1:].isalnum() else "")
    body = []
    if winners:
        body.append(("🏆 " if outcome else "🙅 Faders win: ") + ", ".join(lines))
    if loss_lines:
        body.append("💀 " + ", ".join(loss_lines))
    if not losers and not outcome:
        body.append("Nobody faded it, so it's just a miss for the record.")
    if debt_lines:
        body.append("💸 " + "\n💸 ".join(debt_lines))
        if call["stake_ccy"] in ONCHAIN:
            body.append("Pay, then post the receipt: /callit paid <debt#> <tx signature>")
        else:
            body.append("When you've received it: /callit received <debt#>")
    return head + "\n" + "\n".join(body)


def _bump(conn, call: dict, e: dict, *, won: bool, base: int) -> dict:
    actor = Actor(e["user_id"], e["user_name"])
    _player(conn, call["league"], call["season"], actor)
    row = conn.execute("SELECT * FROM players WHERE league=? AND season=? AND user_id=?",
                       (call["league"], call["season"], e["user_id"])).fetchone()
    streak = row["streak"] + 1 if won else 0
    delta = base + (min(10, 2 * (streak - 1)) if won and streak > 1 else 0)
    author_hit = 1 if (won and e["user_id"] == call["author_id"]) else 0
    conn.execute("UPDATE players SET points=points+?, wins=wins+?, losses=losses+?, streak=?, best_streak=MAX(best_streak, ?), "
                 "calls_hit=calls_hit+? WHERE league=? AND season=? AND user_id=?",
                 (delta, 1 if won else 0, 0 if won else 1, streak, streak, author_hit,
                  call["league"], call["season"], e["user_id"]))
    return {"delta": delta, "streak": streak}


def _make_debts(conn, call: dict, winners: list, losers: list, now: datetime) -> list[str]:
    amt, ccy = call["stake_amount"], call["stake_ccy"]
    if not amt or not winners or not losers or ccy == "PTS":
        return []
    # every loser's stake is split evenly between the winners; then net it down to few transfers
    balance: dict[str, float] = {}
    names = {e["user_id"]: e["user_name"] for e in winners + losers}
    for lo in losers:
        balance[lo["user_id"]] = balance.get(lo["user_id"], 0) - amt
    share = amt * len(losers) / len(winners)
    for w in winners:
        balance[w["user_id"]] = balance.get(w["user_id"], 0) + share
    out = []
    for frm, to, value in net_transfers(balance):
        cur = conn.execute("INSERT INTO debts(call_id, league, from_id, from_name, to_id, to_name, amount, ccy, created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           (call["id"], call["league"], frm, names[frm], to, names[to], round(value, 6), ccy, iso(now)))
        out.append(f"#{cur.lastrowid}: {names[frm]} → {names[to]} {money(value, ccy)}")
    return out


def net_transfers(balance: dict[str, float]) -> list[tuple[str, str, float]]:
    """Greedy settle-up: largest debtor pays largest creditor until everyone is square."""
    debtors = sorted([[k, -v] for k, v in balance.items() if v < -1e-9], key=lambda x: -x[1])
    creditors = sorted([[k, v] for k, v in balance.items() if v > 1e-9], key=lambda x: -x[1])
    out = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        out.append((debtors[i][0], creditors[j][0], pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] <= 1e-9:
            i += 1
        if creditors[j][1] <= 1e-9:
            j += 1
    return out


# ====================================================================== money: wallets + receipts

def set_wallet(actor: Actor, address: str) -> dict:
    address = address.strip()
    if not oracles.valid_address(address):
        return {"ok": False, "message": "That doesn't look like a Solana address."}
    conn = store.db()
    with store.lock():
        conn.execute("INSERT INTO wallets(user_id, address, updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET address=excluded.address, updated_at=excluded.updated_at",
                     (actor.user_id, address, iso(utcnow())))
        conn.commit()
    return {"ok": True, "message": f"👛 {actor.name}'s payout wallet saved: {address[:4]}…{address[-4:]}"}


def _wallet(user_id: str) -> Optional[str]:
    r = store.db().execute("SELECT address FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    return r["address"] if r else None


def mark_paid(league: str, debt_id: int, actor: Actor, tx: str, *, settings: Settings = Settings(),
              fetch: oracles.Fetch = oracles.http_fetch) -> dict:
    conn = store.db()
    d = conn.execute("SELECT * FROM debts WHERE id=? AND league=?", (debt_id, league)).fetchone()
    if not d:
        return {"ok": False, "message": f"No debt #{debt_id} in this chat."}
    if d["from_id"] != actor.user_id:
        return {"ok": False, "message": f"Debt #{debt_id} is {d['from_name']}'s to pay, not yours."}
    if d["status"] != "owed":
        return {"ok": False, "message": f"Debt #{debt_id} is already {d['status']}."}
    if d["ccy"] not in ONCHAIN:
        return {"ok": False, "message": f"{d['ccy']} can't be checked on-chain. Ask {d['to_name']} to confirm: /callit received {debt_id}"}
    if not oracles.valid_signature(tx):
        return {"ok": False, "message": "That doesn't look like a Solana transaction signature."}
    if conn.execute("SELECT 1 FROM debts WHERE tx=?", (tx,)).fetchone():
        return {"ok": False, "message": "That receipt was already used for another debt 🧐"}
    sender, receiver = _wallet(d["from_id"]), _wallet(d["to_id"])
    if not sender or not receiver:
        missing = d["from_name"] if not sender else d["to_name"]
        return {"ok": False, "message": f"{missing} needs to save a wallet first: /callit wallet <solana address>"}
    check = oracles.verify_solana_payment(settings.rpc_url, tx, sender, receiver, d["ccy"], d["amount"], fetch)
    if not check["ok"]:
        return {"ok": False, "message": f"❌ Receipt for #{debt_id} didn't check out: {check['reason']}."}
    with store.lock():
        conn.execute("UPDATE debts SET status='paid', tx=?, verified=1, paid_at=? WHERE id=?", (tx, iso(utcnow()), debt_id))
        _log(conn, league, d["call_id"], "paid", {"debt": debt_id, "tx": tx})
        conn.commit()
    return {"ok": True, "message": (f"✅ Verified on Solana: {d['from_name']} paid {d['to_name']} {money(d['amount'], d['ccy'])} "
                                    f"(debt #{debt_id}).\nhttps://solscan.io/tx/{tx}")}


def mark_received(league: str, debt_id: int, actor: Actor) -> dict:
    conn = store.db()
    d = conn.execute("SELECT * FROM debts WHERE id=? AND league=?", (debt_id, league)).fetchone()
    if not d:
        return {"ok": False, "message": f"No debt #{debt_id} in this chat."}
    if d["to_id"] != actor.user_id:
        return {"ok": False, "message": f"Only {d['to_name']} can confirm they received #{debt_id}."}
    if d["status"] != "owed":
        return {"ok": False, "message": f"Debt #{debt_id} is already {d['status']}."}
    with store.lock():
        conn.execute("UPDATE debts SET status='confirmed', paid_at=? WHERE id=?", (iso(utcnow()), debt_id))
        _log(conn, league, d["call_id"], "received", {"debt": debt_id})
        conn.commit()
    return {"ok": True, "message": f"🤝 {d['to_name']} confirmed {d['from_name']} paid {money(d['amount'], d['ccy'])}. Square!"}


# ====================================================================== views

def _table_rows(league: str, season: int) -> list[dict]:
    return [dict(r) for r in store.db().execute(
        "SELECT * FROM players WHERE league=? AND season=? AND (wins+losses)>0 ORDER BY points DESC, wins DESC, losses ASC",
        (league, season))]


def view_table(league: str) -> str:
    lg = ensure_league(league)
    rows = _table_rows(league, lg["season"])
    if not rows:
        return f"🏆 {lg['season_name']} table is empty. Make the first call: /callit <prediction> by <when>"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 {lg['season_name']} · CALLIT table"]
    for i, r in enumerate(rows[:15]):
        acc = round(100 * r["wins"] / max(1, r["wins"] + r["losses"]))
        fire = f" 🔥{r['streak']}" if r["streak"] >= 2 else ""
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {r['name']} · {r['points']} pts · {r['wins']}-{r['losses']} ({acc}%){fire}")
    if rows[0]["points"] > 0:
        lines.append(f"\n🔮 Prophet of the season so far: {rows[0]['name']}")
    return "\n".join(lines)


def view_board(league: str, settings: Settings = Settings()) -> str:
    calls = [c for c in due_calls(league)]
    if not calls:
        return "📭 No open calls. Start one: /callit <prediction> by <when>"
    calls.sort(key=lambda c: c["deadline"])
    out = ["📋 Open calls"]
    for c in calls[:12]:
        ents = entries(c["id"])
        w = sum(e["side"] == "with" for e in ents)
        a = len(ents) - w
        st = "🔒" if c["status"] == "locked" else "🟢"
        stake = f" · 💰{money(c['stake_amount'], c['stake_ccy'])}" if c["stake_amount"] else ""
        game = c["spec"].get("game", "call")
        if game == "duel":
            who = f"{c['author_name']} vs @{c['spec']['opponent']}" + ("" if len(ents) > 1 else " (waiting)")
            out.append(f"⚔️ #{c['id']} “{c['text']}” · {who}{stake} · ends {fmt_time(dt(c['deadline']), settings.tz)}")
        elif game == "pool":
            n = store.db().execute("SELECT COUNT(*) FROM guesses WHERE call_id=?", (c["id"],)).fetchone()[0]
            out.append(f"🎯 #{c['id']} “{c['text']}” · {n} guesses{stake} · ends {fmt_time(dt(c['deadline']), settings.tz)}")
        else:
            out.append(f"{st} #{c['id']} “{c['text']}” ({c['author_name']}) · {w}🤝 {a}🙅{stake} · ends {fmt_time(dt(c['deadline']), settings.tz)}")
    return "\n".join(out)


def view_stats(league: str, user_id: str, name: str) -> str:
    lg = ensure_league(league)
    r = store.db().execute("SELECT * FROM players WHERE league=? AND season=? AND user_id=?",
                           (league, lg["season"], user_id)).fetchone()
    total = store.db().execute(
        "SELECT SUM(wins) w, SUM(losses) l, MAX(best_streak) b, SUM(calls_made) m, SUM(calls_hit) h FROM players WHERE league=? AND user_id=?",
        (league, user_id)).fetchone()
    if not r and not (total and total["m"]):
        return f"{name} hasn't played yet. First call is free: /callit <prediction> by <when>"
    rank = next((i + 1 for i, x in enumerate(_table_rows(league, lg["season"])) if x["user_id"] == user_id), None)
    last = store.db().execute(
        "SELECT c.id, c.text, c.status, e.side FROM entries e JOIN calls c ON c.id=e.call_id "
        "WHERE c.league=? AND e.user_id=? AND c.status IN ('hit','miss') ORDER BY c.settled_at DESC LIMIT 5",
        (league, user_id)).fetchall()
    form = "".join("✅" if (x["status"] == "hit") == (x["side"] == "with") else "❌" for x in last) or "—"
    owed = store.db().execute("SELECT COUNT(*) n FROM debts WHERE league=? AND from_id=? AND status='owed'",
                              (league, user_id)).fetchone()["n"]
    lines = [f"📇 {name}"]
    if r:
        lines.append(f"{lg['season_name']}: {r['points']} pts · {r['wins']}-{r['losses']}" + (f" · rank #{rank}" if rank else "")
                     + (f" · 🔥{r['streak']} streak" if r["streak"] >= 2 else ""))
    lines.append(f"All time: {total['w'] or 0}-{total['l'] or 0} · own calls hit {total['h'] or 0}/{total['m'] or 0} · best streak {total['b'] or 0}")
    lines.append(f"Last 5: {form}")
    if owed:
        lines.append(f"⚠️ Owes {owed} unpaid debt{'s' if owed > 1 else ''} (/callit debts)")
    return "\n".join(lines)


def view_shame(league: str) -> str:
    conn = store.db()
    lg = ensure_league(league)
    worst = conn.execute(
        "SELECT c.id, c.text, c.author_name, c.result_note, "
        "(SELECT COUNT(*) FROM entries e WHERE e.call_id=c.id AND e.side='against') faders "
        "FROM calls c WHERE c.league=? AND c.status='miss' ORDER BY faders DESC, c.settled_at DESC LIMIT 3", (league,)).fetchall()
    cold = conn.execute("SELECT name, losses FROM players WHERE league=? AND season=? AND losses>0 AND streak=0 "
                        "ORDER BY losses DESC, points ASC LIMIT 3", (league, lg["season"])).fetchall()
    unpaid = conn.execute("SELECT from_name, COUNT(*) n, ccy, SUM(amount) amt FROM debts WHERE league=? AND status='owed' "
                          "GROUP BY from_id, ccy ORDER BY amt DESC LIMIT 5", (league,)).fetchall()
    chickens = conn.execute("SELECT detail FROM log WHERE league=? AND event='chicken' ORDER BY id DESC LIMIT 5",
                            (league,)).fetchall()
    if not (worst or cold or unpaid or chickens):
        return "🧼 Hall of Shame is empty. For now."
    out = ["🤡 Hall of Shame"]
    if worst:
        out.append("Worst calls:")
        out += [f"  #{w['id']} “{w['text']}” ({w['author_name']}) · {w['result_note']}" for w in worst]
    if cold:
        out.append("Coldest players: " + ", ".join(f"{c['name']} ({c['losses']} L)" for c in cold))
    if chickens:
        out.append("🐔 Ran from a duel: " + ", ".join("@" + json.loads(c["detail"]).get("who", "?") for c in chickens))
    if unpaid:
        out.append("Still owing 👀: " + ", ".join(f"{u['from_name']} {money(u['amt'], u['ccy'])}" for u in unpaid))
    return "\n".join(out)


def view_debts(league: str) -> str:
    rows = store.db().execute("SELECT * FROM debts WHERE league=? ORDER BY status='owed' DESC, id DESC LIMIT 15", (league,)).fetchall()
    if not rows:
        return "💸 No money on the line yet. Add 'stake 5 USDC' (or ₦, SOL, USDT) to any call."
    icon = {"owed": "⏳", "paid": "✅", "confirmed": "🤝"}
    out = ["💸 Stakes ledger"]
    for d in rows:
        tag = " (on-chain)" if d["verified"] else ""
        out.append(f"{icon.get(d['status'], '•')} #{d['id']} {d['from_name']} → {d['to_name']} {money(d['amount'], d['ccy'])} · call #{d['call_id']} · {d['status']}{tag}")
    return "\n".join(out)


def view_call(league: str, cid: int, settings: Settings = Settings()) -> str:
    c = get_call(cid)
    if not c or c["league"] != league:
        return f"There's no call #{cid} in this chat."
    ents = entries(cid)
    w = [e["user_name"] for e in ents if e["side"] == "with"]
    a = [e["user_name"] for e in ents if e["side"] == "against"]
    status = {"open": "🟢 open", "locked": "🔒 locked", "hit": "✅ called it", "miss": "❌ wrong", "void": "🤷 void"}[c["status"]]
    lines = [f"#{cid} “{c['text']}” by {c['author_name']} · {status}",
             f"⏳ {fmt_time(dt(c['deadline']), settings.tz)} · sides close {fmt_time(dt(c['lock_at']), settings.tz)}",
             f"🤝 {', '.join(w) or '—'}", f"🙅 {', '.join(a) or '—'}"]
    if c["stake_amount"]:
        lines.append(f"💰 {money(c['stake_amount'], c['stake_ccy'])} each")
    if c["spec"].get("game") == "pool":
        gs = store.db().execute("SELECT user_name, value FROM guesses WHERE call_id=? ORDER BY at", (cid,)).fetchall()
        lines = lines[:2]
        if c["status"] == "open":
            lines.append(f"🎯 {len(gs)} guesses so far (hidden until guesses close)")
        else:
            lines.append("🎯 " + (", ".join(f"{g['user_name']} {g['value']}" for g in gs) or "no guesses"))
    elif c["spec"].get("game") == "duel":
        lines[2] = f"⚔️ {c['author_name']} vs @{c['spec']['opponent']}"
        lines.pop(3)
    if c["result_note"]:
        lines.append(f"📊 {c['result_note']}")
    return "\n".join(lines)


def audit(league: str, cid: int) -> str:
    rows = store.db().execute("SELECT at, event, detail FROM log WHERE league=? AND call_id=? ORDER BY id", (league, cid)).fetchall()
    if not rows:
        return f"No history for #{cid} in this chat."
    out = [f"🧾 Receipts for #{cid} (every change, in order)"]
    for r in rows:
        try:
            d = json.loads(r["detail"])
        except Exception:
            d = r["detail"]
        brief = d if isinstance(d, str) else ", ".join(f"{k}={v}" for k, v in d.items() if k != "spec")
        out.append(f"{r['at'][:16].replace('T', ' ')}Z · {r['event']} · {brief}")
    return "\n".join(out)


# ====================================================================== duels (1v1)

def is_opponent(call: dict, actor: Actor) -> bool:
    opp = str(call["spec"].get("opponent", "")).lower().lstrip("@")
    return bool(opp) and opp in {actor.name.lower().lstrip("@"), actor.user_id.lower(), actor.user_id.split(":")[-1].lower()}


def _duel_card(call: dict, note: str, settings: Settings) -> str:
    cid, opp = call["id"], call["spec"]["opponent"]
    stake = f"\n💰 Stake: {money(call['stake_amount'], call['stake_ccy'])} each" if call["stake_amount"] else ""
    how = {"price": "I settle it from the live price.", "match": "I settle it from the final score."}.get(
        call["kind"], (f"Judge: {call['judge']}." if call.get("judge") else
                       f"After it's played: /callit won {cid} or /callit lost {cid}. If you both claim the win, the group votes."))
    return (f"⚔️ DUEL #{cid}: {call['author_name']} challenges @{opp}\n“{call['text']}”\n"
            f"{(note + chr(10)) if note else ''}"
            f"⏳ Deadline: {fmt_time(dt(call['deadline']), settings.tz)}{stake}\n"
            f"⚖️ {how}\n\n"
            f"@{opp}, you have until {fmt_time(dt(call['lock_at']), settings.tz)}:\n"
            f"/callit accept {cid}   ·   /callit decline {cid}")


def accept(league: str, cid: int, actor: Actor, *, settings: Settings = Settings(), now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    call = get_call(cid)
    if not call or call["league"] != league or call["spec"].get("game") != "duel":
        return {"ok": False, "message": f"There's no duel #{cid} in this chat."}
    if not is_opponent(call, actor):
        return {"ok": False, "message": f"Only @{call['spec']['opponent']} can accept duel #{cid}."}
    if call["status"] != "open" or now >= dt(call["lock_at"]):
        return {"ok": False, "message": f"Duel #{cid} is no longer open."}
    conn = store.db()
    with store.lock():
        if conn.execute("SELECT 1 FROM entries WHERE call_id=? AND user_id=?", (cid, actor.user_id)).fetchone():
            return {"ok": False, "message": f"You already accepted #{cid}."}
        conn.execute("INSERT INTO entries(call_id, user_id, user_name, side, joined_at) VALUES (?,?,?,?,?)",
                     (cid, actor.user_id, actor.name, "against", iso(now)))
        conn.execute("UPDATE calls SET status='locked' WHERE id=?", (cid,))
        _player(conn, league, call["season"], actor)
        _log(conn, league, cid, "accept", {"by": actor.name}, now)
        conn.commit()
    stake = f" for {money(call['stake_amount'], call['stake_ccy'])} each" if call["stake_amount"] else ""
    after = ("" if call["kind"] in ("price", "match") or call.get("judge")
             else f"\nWhen it's done: /callit won {cid} or /callit lost {cid}")
    return {"ok": True, "message": f"🤝 DUEL ON! {call['author_name']} vs {actor.name}{stake}.\n“{call['text']}”{after}"}


def decline(league: str, cid: int, actor: Actor, *, settings: Settings = Settings(), now: Optional[datetime] = None) -> dict:
    call = get_call(cid)
    if not call or call["league"] != league or call["spec"].get("game") != "duel":
        return {"ok": False, "message": f"There's no duel #{cid} in this chat."}
    if not is_opponent(call, actor) and actor.user_id != call["author_id"]:
        return {"ok": False, "message": f"Only the two duelists can call off #{cid}."}
    if call["status"] != "open":
        return {"ok": False, "message": f"Duel #{cid} is already {call['status']}."}
    who = "withdrew" if actor.user_id == call["author_id"] else "declined"
    return {"ok": True, "message": f"🏳️ {actor.name} {who} duel #{cid}.\n" +
            settle(call, None, f"{actor.name} {who}", settings=settings, now=now)}


def report(league: str, cid: int, actor: Actor, says: str, *, settings: Settings = Settings(),
           now: Optional[datetime] = None) -> dict:
    """A player reports the result of a custom call or duel: 'won' or 'lost'.

    A concession settles at once. Two players both claiming the win opens a dispute that the
    group (or the judge) decides with /callit vote.
    """
    now = now or utcnow()
    says = {"won": "won", "win": "won", "w": "won", "lost": "lost", "lose": "lost", "l": "lost",
            "concede": "lost", "conceded": "lost", "gg": "lost"}.get(says.lower())
    if not says:
        return {"ok": False, "message": "Report won or lost."}
    call = get_call(cid)
    if not call or call["league"] != league:
        return {"ok": False, "message": f"There's no call #{cid} in this chat."}
    if call["kind"] != "custom" or call["spec"].get("game") == "pool":
        return {"ok": False, "message": f"#{cid} settles itself from live data 🤖"}
    if call["status"] not in ("open", "locked"):
        return {"ok": False, "message": f"#{cid} is already settled."}
    mine = next((e for e in entries(cid) if e["user_id"] == actor.user_id), None)
    if not mine:
        return {"ok": False, "message": f"You're not playing #{cid}. Spectators can vote if it's disputed."}
    if call["spec"].get("game") == "duel" and len(entries(cid)) < 2:
        return {"ok": False, "message": f"Duel #{cid} hasn't been accepted yet."}
    conn = store.db()
    with store.lock():
        conn.execute("INSERT INTO reports(call_id, user_id, user_name, says, at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(call_id, user_id) DO UPDATE SET says=excluded.says, at=excluded.at",
                     (cid, actor.user_id, actor.name, says, iso(now)))
        _log(conn, league, cid, "report", {"by": actor.name, "says": says}, now)
        conn.commit()
    reps = {r["user_id"]: r["says"] for r in conn.execute("SELECT * FROM reports WHERE call_id=?", (cid,))}
    ents = entries(cid)
    for side in ("with", "against"):
        team = [e for e in ents if e["side"] == side]
        if team and all(reps.get(e["user_id"]) == "lost" for e in team):
            outcome = side == "against"   # this side conceded, so the other side wins
            names = ", ".join(e["user_name"] for e in team)
            return {"ok": True, "message": settle(call, outcome, f"{names} conceded 🫡", settings=settings, now=now)}
    winners_claimed = {e["side"] for e in ents if reps.get(e["user_id"]) == "won"}
    if winners_claimed == {"with", "against"} and not call["spec"].get("disputed"):
        spec = dict(call["spec"], disputed=True)
        conn.execute("UPDATE calls SET spec=? WHERE id=?", (json.dumps(spec), cid))
        conn.commit()
        judge = f"Judge {call['judge']}, your call: " if call.get("judge") else "Group, settle it: "
        return {"ok": True, "message": f"🚨 Both sides claim the win on #{cid}. {judge}/callit vote {cid} yes|no\n"
                                       f"(yes = {call['author_name']} was right · people not playing count double)"}
    return {"ok": True, "message": f"📝 {actor.name} says they {says} #{cid}. Waiting for the other side."}


# ====================================================================== pools (closest guess wins)

_POOL_PRICE = re.compile(r"^\$?(?P<sym>[a-z][a-z0-9]{1,11})(?:\s+(?:price|closes?|close price|closing price))?$", re.I)
_POOL_SCORE = re.compile(r"^(?P<a>[\w .'&-]+?)\s+(?:vs\.?|v|x|and)\s+(?P<b>[\w .'&-]+?)(?:\s+(?:final\s+)?score)?$", re.I)
_SCORE_VAL = re.compile(r"^\s*(\d{1,2})\s*[-:–]\s*(\d{1,2})\s*$")
POOL_SPLIT = {1: [1.0], 2: [1.0], 3: [0.5, 0.3, 0.2]}
POOL_POINTS = [30, 20, 10]


def make_pool(league: str, actor: Actor, raw: str, *, settings: Settings = Settings(),
              now: Optional[datetime] = None, fetch: oracles.Fetch = oracles.http_fetch) -> dict:
    now = now or utcnow()
    from .parse import extract_deadline, extract_judge, extract_stake
    text = re.sub(r"\s+", " ", raw).strip()
    text, stake = extract_stake(text)
    text, judge = extract_judge(text)
    text, deadline = extract_deadline(text, now, settings.tz)
    text = text.strip(" ,.;?")
    if not text:
        return {"ok": False, "message": "What's the pool about? e.g. /callit pool BTC price by Friday 6pm"}
    spec: dict = {"game": "pool"}
    kind, kickoff, note = "custom", None, ""
    m = _POOL_PRICE.match(text)
    sc = _POOL_SCORE.match(text)
    if m and not sc:
        coin = oracles.resolve_coin(m.group("sym"), fetch)
        if coin:
            live = oracles.price_now(coin["id"], fetch)
            kind = "price"
            spec.update(symbol=coin["symbol"], coin_id=coin["id"], start_price=live)
            deadline = deadline or now + timedelta(hours=24)
            note = f"Guess {coin['symbol']}'s price at the deadline. It's ${live:,.6g} now." if live else ""
    elif sc:
        fx = oracles.find_fixture(sc.group("a"), sc.group("b"), now, fetch=fetch)
        if fx:
            kind, kickoff = "match", dt(fx["kickoff"])
            spec.update(fx)
            deadline = kickoff + MATCH_RESULT_DELAY
            note = f"Guess the final score of {fx['name']} (home-away, e.g. 2-1). Kicks off {fmt_time(kickoff, settings.tz)}."
    if kind == "custom":
        if not judge:
            judge = actor.name
            spec["host_judges"] = True
        deadline = deadline or now + timedelta(hours=24)
        note = f"Guess a number. {judge} enters the real answer: /callit result <id> <number>."
    if deadline <= now:
        return {"ok": False, "message": "That deadline has already passed."}
    lock_at = kickoff or lock_time(now, deadline)
    lg = ensure_league(league)
    conn = store.db()
    with store.lock():
        cur = conn.execute(
            "INSERT INTO calls(league, season, author_id, author_name, text, kind, spec, created_at, lock_at, deadline, "
            "stake_amount, stake_ccy, judge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (league, lg["season"], actor.user_id, actor.name, text, kind, json.dumps(spec), iso(now), iso(lock_at),
             iso(deadline), stake.amount if stake else None, stake.currency if stake else None, judge))
        cid = cur.lastrowid
        _player(conn, league, lg["season"], actor)
        _log(conn, league, cid, "pool", {"by": actor.name, "text": text, "kind": kind}, now)
        conn.commit()
    pot = f"\n💰 Entry: {money(stake.amount, stake.currency)} each · top 3 split the pot 50/30/20" if stake else ""
    host = "\n(The host judges this one, so they can't guess.)" if spec.get("host_judges") else ""
    return {"ok": True, "call_id": cid, "message": (
        f"🎯 POOL #{cid} by {actor.name}\n“{text}”\n{(note + chr(10)) if note else ''}"
        f"⏳ Result: {fmt_time(deadline, settings.tz)}\n🔒 Guesses close: {fmt_time(lock_at, settings.tz)}{pot}\n"
        f"🏆 Closest 3 win: +30 · +20 · +10 pts{host}\n\nYour guess: /callit guess {cid} <answer>")}


def _parse_guess(call: dict, value: str) -> Optional[tuple]:
    if call["kind"] == "match":
        m = _SCORE_VAL.match(value)
        return (float(m.group(1)), float(m.group(2))) if m else None
    try:
        v = value.strip().lower().replace(",", "").lstrip("$₦")
        mult = 1000 if v.endswith("k") else 1_000_000 if v.endswith("m") else 1
        return (float(v.rstrip("km")) * mult, None)
    except ValueError:
        return None


def guess(league: str, cid: int, actor: Actor, value: str, *, settings: Settings = Settings(),
          now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    call = get_call(cid)
    if not call or call["league"] != league or call["spec"].get("game") != "pool":
        return {"ok": False, "message": f"There's no pool #{cid} in this chat."}
    if call["status"] != "open" or now >= dt(call["lock_at"]):
        return {"ok": False, "message": f"Guesses on #{cid} are closed 🔒"}
    if call["spec"].get("host_judges") and actor.user_id == call["author_id"]:
        return {"ok": False, "message": "You're the judge of this pool, so no guessing 😉"}
    parsed = _parse_guess(call, value)
    if not parsed:
        hint = "a score like 2-1" if call["kind"] == "match" else "a number like 312.5 or 95k"
        return {"ok": False, "message": f"That guess needs to be {hint}."}
    conn = store.db()
    with store.lock():
        if conn.execute("SELECT 1 FROM guesses WHERE call_id=? AND user_id=?", (cid, actor.user_id)).fetchone():
            return {"ok": False, "message": "One guess each, no changes 🔒"}
        taken = conn.execute("SELECT user_name FROM guesses WHERE call_id=? AND num=? AND IFNULL(num2,-1)=IFNULL(?,-1)",
                             (cid, parsed[0], parsed[1])).fetchone()
        if taken:
            return {"ok": False, "message": f"{taken['user_name']} already took {value}. Pick a different answer."}
        conn.execute("INSERT INTO guesses(call_id, user_id, user_name, value, num, num2, at) VALUES (?,?,?,?,?,?,?)",
                     (cid, actor.user_id, actor.name, value.strip(), parsed[0], parsed[1], iso(now)))
        _player(conn, league, call["season"], actor)
        _log(conn, league, cid, "guess", {"by": actor.name, "value": value.strip()}, now)
        conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM guesses WHERE call_id=?", (cid,)).fetchone()[0]
    return {"ok": True, "message": f"🎯 {actor.name} guessed {value.strip()} on #{cid}. {n} in the pool."}


def pool_result(league: str, cid: int, actor: Actor, value: str, *, settings: Settings = Settings(),
                now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    call = get_call(cid)
    if not call or call["league"] != league or call["spec"].get("game") != "pool" or call["kind"] != "custom":
        return {"ok": False, "message": f"#{cid} isn't a pool that needs a judge."}
    judge = (call.get("judge") or "").lower().lstrip("@")
    if judge not in {actor.name.lower().lstrip("@"), actor.user_id.lower()}:
        return {"ok": False, "message": f"Only {call['judge']} can enter the result for #{cid}."}
    if now < dt(call["lock_at"]):
        return {"ok": False, "message": "Guesses are still open. Enter the result after they close."}
    parsed = _parse_guess(call, value)
    if not parsed:
        return {"ok": False, "message": "The result needs to be a number."}
    return {"ok": True, "message": settle_pool(call, parsed, f"{actor.name} entered the answer: {value.strip()}",
                                               settings=settings, now=now)}


def _referee_pool(call: dict, settings: Settings, now: datetime, fetch) -> Optional[str]:
    deadline = dt(call["deadline"])
    spec = call["spec"]
    if call["kind"] == "price" and now >= deadline:
        pts = oracles.price_range(spec["coin_id"], deadline - timedelta(hours=2), deadline + timedelta(minutes=10), fetch)
        before = [x for x in pts if x[0] <= deadline + timedelta(minutes=5)]
        p = (before or pts)[-1][1] if pts else (oracles.price_now(spec["coin_id"], fetch) if now - deadline < timedelta(minutes=10) else None)
        if p is not None:
            return settle_pool(call, (p, None), f"{spec['symbol']} was ${p:,.6g} at the deadline", settings=settings, now=now)
    elif call["kind"] == "match" and now >= deadline:
        res = oracles.match_result(spec, fetch)
        if res and res.get("final") and not res.get("void"):
            return settle_pool(call, (float(res["home_score"]), float(res["away_score"])),
                               f"Final: {res['home']} {res['home_score']}-{res['away_score']} {res['away']}",
                               settings=settings, now=now)
    if now - deadline > VOID_AFTER:
        return settle(call, None, "no result came in", settings=settings, now=now)
    return None


def _distance(call: dict, g: tuple, answer: tuple) -> float:
    if call["kind"] == "match":
        sign = lambda h, a: (h > a) - (h < a)
        return abs(g[0] - answer[0]) + abs(g[1] - answer[1]) + (0 if sign(*g) == sign(*answer) else 0.5)
    return abs(g[0] - answer[0])


def settle_pool(call: dict, answer: tuple, note: str, *, settings: Settings = Settings(),
                now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    conn = store.db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM guesses WHERE call_id=? ORDER BY at", (call["id"],))]
    if not rows:
        return settle(call, None, "nobody guessed", settings=settings, now=now)
    ranked = sorted(rows, key=lambda r: (_distance(call, (r["num"], r["num2"]), answer), r["at"]))
    with store.lock():
        fresh = conn.execute("SELECT status FROM calls WHERE id=?", (call["id"],)).fetchone()
        if fresh["status"] not in ("open", "locked"):
            return ""
        conn.execute("UPDATE calls SET status='hit', outcome='pool', result_note=?, settled_at=? WHERE id=?",
                     (note, iso(now), call["id"]))
        _log(conn, call["league"], call["id"], "settle", {"status": "pool", "note": note}, now)
        lines, medals = [], ["🥇", "🥈", "🥉"]
        n_win = min(3, len(ranked))
        for i, r in enumerate(ranked):
            e = {"user_id": r["user_id"], "user_name": r["user_name"]}
            won = i < n_win and len(ranked) > 1
            p = _bump(conn, call, e, won=won, base=POOL_POINTS[i] if won else -2)
            if i < n_win:
                lines.append(f"{medals[i]} {r['user_name']} · {r['value']} (+{p['delta']})" if won else f"{medals[i]} {r['user_name']} · {r['value']}")
        debt_lines = []
        amt, ccy = call["stake_amount"], call["stake_ccy"]
        if amt and len(ranked) > 1 and ccy != "PTS":
            split = POOL_SPLIT.get(len(ranked), POOL_SPLIT[3])
            pot = amt * len(ranked)
            balance = {r["user_id"]: -amt for r in ranked}
            for i, share in enumerate(split[:len(ranked)]):
                balance[ranked[i]["user_id"]] += pot * share
            names = {r["user_id"]: r["user_name"] for r in ranked}
            for frm, to, value in net_transfers(balance):
                cur = conn.execute("INSERT INTO debts(call_id, league, from_id, from_name, to_id, to_name, amount, ccy, created_at) "
                                   "VALUES (?,?,?,?,?,?,?,?,?)",
                                   (call["id"], call["league"], frm, names[frm], to, names[to], round(value, 6), ccy, iso(now)))
                debt_lines.append(f"#{cur.lastrowid}: {names[frm]} → {names[to]} {money(value, ccy)}")
        conn.commit()
    out = f"🏁 POOL #{call['id']} settled\n“{call['text']}”\n📊 {note}\n" + "\n".join(lines)
    if len(ranked) == 1:
        out += "\nOnly one guess, so no points this time."
    if len(ranked) > 3:
        out += f"\n💀 {len(ranked) - 3} others missed (-2)"
    if debt_lines:
        out += "\n💸 " + "\n💸 ".join(debt_lines)
        out += ("\nPay, then post the receipt: /callit paid <debt#> <tx signature>" if ccy in ONCHAIN
                else "\nWhen you've received it: /callit received <debt#>")
    return out
