"""CALLIT — a prediction league for any Hermes group chat.

Surfaces used (all public plugin APIs):
  * ``/callit`` slash command (Telegram, Discord, Slack, WhatsApp, Signal, CLI, Desktop)
  * five agent tools in the ``callit`` toolset, so people can just talk ("I bet SOL flips 300 by Friday")
  * a system-prompt section telling the agent the league exists
  * an optional auto-referee cron job (``/callit referee on``) created through ``cronjob_manage``
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import engine
from .engine import Actor, Settings

TOOLSET = "callit"

HELP = """🔮 CALLIT · bet your friends on anything

📣 Group call: everyone picks a side
  /callit BTC above 120k by Friday 6pm
  /callit Arsenal beat Chelsea stake 5 USDC
  /callit <anything> by <when> judge @username
  /callit with <id>  ·  /callit against <id>

⚔️ 1v1 duel
  /callit duel @username <your bet> stake ₦2000
  /callit accept <id>  ·  /callit decline <id>
  /callit won <id>  ·  /callit lost <id>

🎯 Pool: closest 3 guesses win
  /callit pool BTC price by Sunday stake 1 USDC
  /callit pool Arsenal vs Chelsea score
  /callit guess <id> <answer>

📊 League
  /callit board · table · shame · me · debts
  /callit show <id> · receipts <id>

⚖️ Results
  Prices and football settle automatically.
  Other bets: the judge decides, or /callit vote <id> yes|no
  /callit referee on  → auto-post results here

💸 Stakes (optional, paid directly between players)
  Add "stake 5 USDC", "stake ₦2000" or "stake 0.1 SOL"
  /callit wallet <solana address>
  /callit paid <debt id> <tx signature>
  /callit received <debt id>

You can also just say it: "I bet BTC hits 120k by Sunday" """

_CTX: Any = None


# ---------------------------------------------------------------- identity

def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name) or ""
    except Exception:
        return ""


def _identity(source: Any = None, platform: str = "") -> tuple[str, Actor, dict]:
    """(league key, player, chat meta) for whoever is talking, from Hermes' own session routing."""
    if source is not None:
        plat = str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", "")) or platform)
        chat = str(getattr(source, "chat_id", "") or "")
        thread = str(getattr(source, "thread_id", "") or "")
        uid = str(getattr(source, "user_id", "") or "")
        uname = str(getattr(source, "user_name", "") or "")
        cname = str(getattr(source, "chat_name", "") or "")
    else:
        plat = _session_env("HERMES_SESSION_PLATFORM") or platform
        chat = _session_env("HERMES_SESSION_CHAT_ID")
        thread = _session_env("HERMES_SESSION_THREAD_ID")
        uid = _session_env("HERMES_SESSION_USER_ID")
        uname = _session_env("HERMES_SESSION_USER_NAME")
        cname = _session_env("HERMES_SESSION_CHAT_NAME")
    if not plat or plat in ("cli", "tui", "desktop", "local", "acp") or not chat:
        name = str(_cfg("player_name", "") or "You")
        return "local", Actor("local:you", name), {"platform": "local", "chat_id": "", "thread_id": "", "name": "This device"}
    key = f"{plat}:{chat}" + (f":{thread}" if thread else "")
    actor = Actor(f"{plat}:{uid or uname or 'unknown'}", uname or uid or "Someone")
    return key, actor, {"platform": plat, "chat_id": chat, "thread_id": thread, "name": cname}


def _cfg(key: str, default: Any) -> Any:
    try:
        return _CTX.get_config(key, default=default) if _CTX is not None else default
    except Exception:
        return default


def _settings() -> Settings:
    try:
        mv = int(_cfg("min_votes", 3))
    except Exception:
        mv = 3
    return Settings(tz=str(_cfg("timezone", "UTC") or "UTC"), min_votes=max(1, mv),
                    rpc_url=str(_cfg("solana_rpc_url", "https://api.mainnet-beta.solana.com")))


# ---------------------------------------------------------------- auto referee (cron)

def _referee_prompt(league: str) -> str:
    return ("You are the CALLIT referee for one group chat. Call the callit_referee tool with "
            f"league=\"{league}\". If the tool result's `announcement` field is empty, reply with exactly [SILENT]. "
            "Otherwise reply with the announcement text exactly as given, with no extra words before or after it.")


def _referee_on(league: str, meta: dict) -> str:
    lg = engine.ensure_league(league)
    if lg.get("referee_job"):
        return f"⚖️ Auto-referee is already on for this chat (job {lg['referee_job']})."
    if league == "local":
        return "Auto-referee posts into a group chat. Run /callit referee on inside your Telegram/Discord/WhatsApp group."
    deliver = f"{meta['platform']}:{meta['chat_id']}" + (f":{meta['thread_id']}" if meta.get("thread_id") else "")
    args = {"action": "create", "name": f"CALLIT referee · {meta.get('name') or league}",
            "schedule": str(_cfg("referee_every", "15m") or "15m"), "prompt": _referee_prompt(league),
            "deliver": deliver, "enabled_toolsets": [TOOLSET], "failure_deliver": "local"}
    try:
        raw = _CTX.dispatch_tool("cronjob_manage", args)
        res = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        return f"Couldn't schedule the referee ({exc.__class__.__name__}). You can still use /callit settle any time."
    job = (res or {}).get("job_id")
    if not job:
        return "Couldn't schedule the referee: " + str((res or {}).get("error") or res) + "\nYou can still use /callit settle any time."
    conn = engine.store.db()
    conn.execute("UPDATE leagues SET referee_job=? WHERE key=?", (job, league))
    conn.commit()
    return (f"⚖️ Auto-referee ON. Every {_cfg('referee_every', '15m')} I check prices, scores and votes, "
            "then post results here. Quiet when there's nothing to say.")


def _referee_off(league: str) -> str:
    lg = engine.ensure_league(league)
    job = lg.get("referee_job")
    if not job:
        return "Auto-referee is already off. Results still settle whenever someone uses /callit."
    try:
        _CTX.dispatch_tool("cronjob_manage", {"action": "remove", "job_id": job})
    except Exception:
        pass
    conn = engine.store.db()
    conn.execute("UPDATE leagues SET referee_job=NULL WHERE key=?", (league,))
    conn.commit()
    return "⚖️ Auto-referee OFF."


# ---------------------------------------------------------------- /callit

_ID = r"#?(\d+)"


def run_command(raw: str, league: str, actor: Actor, meta: dict, settings: Optional[Settings] = None) -> str:
    settings = settings or _settings()
    engine.ensure_league(league, meta.get("platform", ""), meta.get("chat_id", ""), meta.get("thread_id", ""), meta.get("name", ""))
    text = (raw or "").strip()

    # local "hotseat" mode: on one device you can play as several friends to try it out
    m = re.match(r"^as\s+(\S+)\s+(.+)$", text, re.I)
    if m and league == "local":
        actor = Actor(f"local:{m.group(1).lower()}", m.group(1))
        text = m.group(2).strip()

    low = text.lower()
    news = engine.referee(league, settings=settings)  # anything due gets settled before we answer
    if news and low in ("settle", "check", "ref", "referee", "referee now"):
        return "\n\n".join(news)
    reply = _dispatch(low, text, league, actor, meta, settings)
    return "\n\n".join(news + [reply]) if news else reply


def _dispatch(low: str, text: str, league: str, actor: Actor, meta: dict, settings: Settings) -> str:
    if low in ("", "help", "?", "how"):
        return HELP
    if m := re.match(r"^duel\s+@?(\S+)\s+(.+)$", text, re.I):
        return engine.make_call(league, actor, m.group(2), settings=settings, mode="duel", opponent=m.group(1))["message"]
    if m := re.match(r"^pool\s+(.+)$", text, re.I):
        return engine.make_pool(league, actor, m.group(1), settings=settings)["message"]
    if m := re.match(rf"^accept\s+{_ID}$", low):
        return engine.accept(league, int(m.group(1)), actor, settings=settings)["message"]
    if m := re.match(rf"^(?:decline|reject|withdraw|cancel)\s+{_ID}$", low):
        return engine.decline(league, int(m.group(1)), actor, settings=settings)["message"]
    if m := re.match(rf"^(won|win|lost|lose|concede|gg)\s+{_ID}$", low):
        return engine.report(league, int(m.group(2)), actor, m.group(1), settings=settings)["message"]
    if m := re.match(rf"^guess\s+{_ID}\s+(.+)$", text, re.I):
        return engine.guess(league, int(m.group(1)), actor, m.group(2), settings=settings)["message"]
    if m := re.match(rf"^result\s+{_ID}\s+(.+)$", text, re.I):
        return engine.pool_result(league, int(m.group(1)), actor, m.group(2), settings=settings)["message"]
    if m := re.match(rf"^(with|back|for|agree|against|fade|doubt|cap)\s+{_ID}$", low):
        return engine.join(league, int(m.group(2)), actor, m.group(1), settings=settings)["message"]
    if m := re.match(rf"^vote\s+{_ID}\s+(\S+)$", low):
        return engine.vote(league, int(m.group(1)), actor, m.group(2), settings=settings)["message"]
    if low in ("board", "open", "calls", "list"):
        return engine.view_board(league, settings)
    if low in ("table", "leaderboard", "lb", "standings", "rank"):
        return engine.view_table(league)
    if low in ("shame", "hall of shame", "wall of shame"):
        return engine.view_shame(league)
    if low in ("me", "stats", "my stats", "record"):
        return engine.view_stats(league, actor.user_id, actor.name)
    if m := re.match(r"^(?:stats|record)\s+@?(\S+)$", low):
        who = m.group(1)
        row = engine.store.db().execute("SELECT user_id, name FROM players WHERE league=? AND (lower(name)=? OR lower(name)=?) LIMIT 1",
                                        (league, who, "@" + who)).fetchone()
        return engine.view_stats(league, row["user_id"], row["name"]) if row else f"No player called {who} here yet."
    if low in ("debts", "ledger", "money", "owed"):
        return engine.view_debts(league)
    if m := re.match(rf"^(?:show|call|info)\s+{_ID}$", low):
        return engine.view_call(league, int(m.group(1)), settings)
    if m := re.match(rf"^(?:receipts|audit|history|log)\s+{_ID}$", low):
        return engine.audit(league, int(m.group(1)))
    if low in ("settle", "check", "ref", "referee", "referee now"):
        return "⚖️ Checked every open call. Nothing new to settle yet."
    if low in ("referee on", "auto on", "autoref on"):
        return _referee_on(league, meta)
    if low in ("referee off", "auto off", "autoref off"):
        return _referee_off(league)
    if m := re.match(r"^season\s+new\s*(.*)$", text, re.I):
        return engine.new_season(league, m.group(1).strip())
    if m := re.match(r"^wallet\s+(\S+)$", text, re.I):
        return engine.set_wallet(actor, m.group(1))["message"]
    if m := re.match(rf"^paid\s+{_ID}\s+(\S+)$", text, re.I):
        return engine.mark_paid(league, int(m.group(1)), actor, m.group(2), settings=settings)["message"]
    if m := re.match(rf"^(?:received|got)\s+{_ID}$", low):
        return engine.mark_received(league, int(m.group(1)), actor)["message"]
    return engine.make_call(league, actor, text, settings=settings)["message"]


def _handle_command(raw_args: str, *, source=None, platform: str = "", **_kw) -> str:
    try:
        league, actor, meta = _identity(source, platform)
        return run_command(raw_args, league, actor, meta)
    except Exception as exc:  # never crash the chat
        return f"CALLIT hit a snag ({exc.__class__.__name__}: {exc}). Try again in a moment."


# ---------------------------------------------------------------- agent tools

def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _tool(fn):
    def handler(args, **kwargs):
        try:
            return fn(args or {}, **kwargs)
        except Exception as exc:
            return _err(f"{exc.__class__.__name__}: {exc}")
    handler.__name__ = fn.__name__
    return handler


RELAY = " Show the returned `message` to the chat as-is (it is already formatted for chat)."


@_tool
def t_make_call(args, **_):
    league, actor, meta = _identity()
    pred = str(args.get("prediction", "")).strip()
    if not pred:
        return _err("prediction is required")
    engine.ensure_league(league, meta["platform"], meta["chat_id"], meta["thread_id"], meta["name"])
    res = engine.make_call(league, actor, pred, settings=_settings())
    return _ok(**{k: v for k, v in res.items() if k in ("ok", "call_id", "message")})


@_tool
def t_duel(args, **_):
    league, actor, meta = _identity()
    engine.ensure_league(league, meta["platform"], meta["chat_id"], meta["thread_id"], meta["name"])
    res = engine.make_call(league, actor, str(args.get("prediction", "")), settings=_settings(),
                           mode="duel", opponent=str(args.get("opponent", "")))
    return _ok(**{k: v for k, v in res.items() if k in ("ok", "call_id", "message")})


@_tool
def t_pool(args, **_):
    league, actor, meta = _identity()
    engine.ensure_league(league, meta["platform"], meta["chat_id"], meta["thread_id"], meta["name"])
    res = engine.make_pool(league, actor, str(args.get("question", "")), settings=_settings())
    return _ok(**{k: v for k, v in res.items() if k in ("ok", "call_id", "message")})


@_tool
def t_play(args, **_):
    league, actor, _m = _identity()
    cid, action, value = int(args["call_id"]), str(args.get("action", "")).lower(), str(args.get("value", ""))
    s = _settings()
    fn = {
        "accept": lambda: engine.accept(league, cid, actor, settings=s),
        "decline": lambda: engine.decline(league, cid, actor, settings=s),
        "won": lambda: engine.report(league, cid, actor, "won", settings=s),
        "lost": lambda: engine.report(league, cid, actor, "lost", settings=s),
        "guess": lambda: engine.guess(league, cid, actor, value, settings=s),
        "result": lambda: engine.pool_result(league, cid, actor, value, settings=s),
    }.get(action)
    return _ok(**fn()) if fn else _err("action must be accept, decline, won, lost, guess or result")


@_tool
def t_join(args, **_):
    league, actor, _m = _identity()
    return _ok(**engine.join(league, int(args["call_id"]), actor, str(args.get("side", "")), settings=_settings()))


@_tool
def t_vote(args, **_):
    league, actor, _m = _identity()
    return _ok(**engine.vote(league, int(args["call_id"]), actor, str(args.get("verdict", "")), settings=_settings()))


@_tool
def t_board(args, **_):
    league, actor, meta = _identity()
    view = str(args.get("view", "open")).lower()
    cid = args.get("call_id")
    s = _settings()
    msg = {
        "open": lambda: engine.view_board(league, s),
        "table": lambda: engine.view_table(league),
        "shame": lambda: engine.view_shame(league),
        "stats": lambda: engine.view_stats(league, actor.user_id, actor.name),
        "debts": lambda: engine.view_debts(league),
        "call": lambda: engine.view_call(league, int(cid), s) if cid else "call_id needed",
        "receipts": lambda: engine.audit(league, int(cid)) if cid else "call_id needed",
    }.get(view, lambda: engine.view_board(league, s))()
    return _ok(message=msg)


@_tool
def t_referee(args, **_):
    league = str(args.get("league") or "").strip()
    if not league:
        league, _a, _m = _identity()
    news = engine.referee(league, settings=_settings())
    return _ok(announcement="\n\n".join(news), settled=len(news))


SCHEMAS = {
    "callit_make_call": {
        "name": "callit_make_call",
        "description": (
            "Record a new CALLIT prediction for the person speaking, in the current chat's league. Use whenever someone "
            "makes a confident prediction or a bet ('I bet...', 'mark my words', 'calling it now', 'X go win'). "
            "Rewrite their words into the canonical form: '<claim> by <deadline> [stake <amount> <currency>] [judge @name]'. "
            "Claims settle automatically when written like 'SOL above 300', 'BTC hits 100k', 'ETH below 2500' (crypto) "
            "or 'Arsenal beat Chelsea' / 'Real Madrid vs Barcelona draw' (football, the deadline is found automatically). "
            "Anything else is a custom call settled by a judge or a group vote and must have a deadline. "
            "Currencies: USDC, USDT, SOL (verified on-chain), NGN/₦, USD/$." + RELAY),
        "parameters": {"type": "object", "properties": {
            "prediction": {"type": "string", "description": "Canonical call text, e.g. 'SOL above 300 by Friday 6pm stake 5 USDC'"}},
            "required": ["prediction"]},
    },
    "callit_duel": {
        "name": "callit_duel",
        "description": ("Challenge one specific person to a 1v1 bet for the person speaking. Use when someone dares or "
                        "challenges a named friend ('I bet you @friend…', 'I beat you in FC tonight'). Same canonical prediction "
                        "format as callit_make_call; video-game and real-life bets work (players report won/lost)." + RELAY),
        "parameters": {"type": "object", "properties": {
            "opponent": {"type": "string", "description": "The challenged person's @username, without @"},
            "prediction": {"type": "string", "description": "e.g. 'I beat you in EA FC by tonight stake ₦2000'"}},
            "required": ["opponent", "prediction"]},
    },
    "callit_pool": {
        "name": "callit_pool",
        "description": ("Start a guessing pool: everyone guesses a number or score, the closest 3 win. Settles itself for "
                        "'<COIN> price by <when>' and '<Team A> vs <Team B> score'; otherwise needs 'judge @name'." + RELAY),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "e.g. 'BTC price by Friday 6pm stake 1 USDC'"}},
            "required": ["question"]},
    },
    "callit_play": {
        "name": "callit_play",
        "description": ("Act on an existing duel or pool for the person speaking: accept/decline a duel, report won/lost, "
                        "guess in a pool, or (judge only) enter a pool's result." + RELAY),
        "parameters": {"type": "object", "properties": {
            "call_id": {"type": "integer"},
            "action": {"type": "string", "enum": ["accept", "decline", "won", "lost", "guess", "result"]},
            "value": {"type": "string", "description": "The guess or result, for guess/result"}},
            "required": ["call_id", "action"]},
    },
    "callit_join": {
        "name": "callit_join",
        "description": "Put the person speaking on a side of an existing call: 'with' (agree) or 'against' (fade). Sides close early and cannot be switched." + RELAY,
        "parameters": {"type": "object", "properties": {
            "call_id": {"type": "integer"}, "side": {"type": "string", "enum": ["with", "against"]}},
            "required": ["call_id", "side"]},
    },
    "callit_vote": {
        "name": "callit_vote",
        "description": "Vote on whether a custom call came true, for the person speaking. Only after its deadline." + RELAY,
        "parameters": {"type": "object", "properties": {
            "call_id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["yes", "no"]}},
            "required": ["call_id", "verdict"]},
    },
    "callit_board": {
        "name": "callit_board",
        "description": "Show the chat's CALLIT league: open calls, season table, hall of shame, the speaker's stats, the stakes ledger, one call, or one call's full audit trail." + RELAY,
        "parameters": {"type": "object", "properties": {
            "view": {"type": "string", "enum": ["open", "table", "shame", "stats", "debts", "call", "receipts"]},
            "call_id": {"type": "integer", "description": "For view=call or view=receipts"}},
            "required": ["view"]},
    },
    "callit_referee": {
        "name": "callit_referee",
        "description": "Check every due CALLIT call against live data (prices, football scores, votes), settle what can be settled and return the announcement for the chat. Used by the auto-referee job; returns an empty announcement when nothing changed.",
        "parameters": {"type": "object", "properties": {
            "league": {"type": "string", "description": "League key (platform:chat_id[:thread]); defaults to the current chat"}},
            "required": []},
    },
}
HANDLERS = {"callit_make_call": t_make_call, "callit_duel": t_duel, "callit_pool": t_pool, "callit_play": t_play, "callit_join": t_join, "callit_vote": t_vote,
            "callit_board": t_board, "callit_referee": t_referee}

PROMPT = ("CALLIT is installed: this chat can run a prediction league. When someone makes a confident prediction or offers a bet, "
          "offer to lock it in (or do it if they ask) with callit_make_call; callit_duel when they challenge a named person, callit_pool for guess-the-number games, callit_play to accept/decline/report/guess; use callit_join when people agree or fade a call, "
          "callit_vote for custom-call verdicts and callit_board for tables and stats. Users can also type /callit help. "
          "Never invent results: CALLIT settles from live data, a named judge or a group vote. Relay tool messages as returned.")


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=HANDLERS[name])
    ctx.register_command("callit", handler=_handle_command,
                         description="Group prediction league: make calls, pick sides, auto-settle, leaderboard",
                         args_hint="<prediction> | with|against <id> | table | help")
    try:
        ctx.register_system_prompt_section("callit", PROMPT)
    except Exception:
        pass
