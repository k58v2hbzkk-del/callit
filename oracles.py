"""Read-only data sources CALLIT uses to settle calls. No API keys, no wallets, no writes.

* CoinGecko public API  - crypto prices (api.coingecko.com)
* ESPN public scoreboard - football fixtures and final scores (site.api.espn.com)
* Solana JSON-RPC        - verifying that a posted payment receipt really happened

Every function takes an optional ``fetch`` callable so tests can run offline.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

UA = "callit-hermes-plugin/1.0 (+https://hermes-agent.nousresearch.com)"
Fetch = Callable[[str, Optional[dict]], Any]

_CACHE: dict[str, tuple[float, Any]] = {}


def _ssl_context() -> ssl.SSLContext:
    try:  # follow Hermes' own trust store when available
        import truststore  # type: ignore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def http_fetch(url: str, body: Optional[dict] = None, timeout: float = 12.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cached(url: str, fetch: Fetch, ttl: float = 60.0) -> Any:
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fetch(url, None)
    _CACHE[url] = (time.time(), val)
    if len(_CACHE) > 256:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:64]:
            _CACHE.pop(k, None)
    return val


# ------------------------------------------------------------------ crypto prices

CG = "https://api.coingecko.com/api/v3"
KNOWN_COINS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "TON": "the-open-network", "TRX": "tron", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "SUI": "sui", "NEAR": "near", "HYPE": "hyperliquid",
    "PEPE": "pepe", "SHIB": "shiba-inu", "WIF": "dogwifcoin", "BONK": "bonk", "LTC": "litecoin",
    "APT": "aptos", "ARB": "arbitrum", "OP": "optimism", "JUP": "jupiter-exchange-solana",
    "CELO": "celo", "USDT": "tether", "USDC": "usd-coin", "TRUMP": "official-trump", "PI": "pi-network",
}


def resolve_coin(symbol: str, fetch: Fetch = http_fetch) -> Optional[dict]:
    sym = symbol.upper().lstrip("$")
    if sym in KNOWN_COINS:
        return {"id": KNOWN_COINS[sym], "symbol": sym}
    data = cached(f"{CG}/search?query={urllib.parse.quote(sym)}", fetch, ttl=3600)
    coins = [c for c in (data or {}).get("coins", []) if str(c.get("symbol", "")).upper() == sym]
    if not coins:
        return None
    coins.sort(key=lambda c: c.get("market_cap_rank") or 10**9)  # biggest coin wins, not a copycat
    return {"id": coins[0]["id"], "symbol": sym, "name": coins[0].get("name")}


def price_now(coin_id: str, fetch: Fetch = http_fetch) -> Optional[float]:
    data = cached(f"{CG}/simple/price?ids={urllib.parse.quote(coin_id)}&vs_currencies=usd", fetch, ttl=30)
    try:
        return float(data[coin_id]["usd"])
    except Exception:
        return None


def price_range(coin_id: str, start: datetime, end: datetime, fetch: Fetch = http_fetch) -> list[tuple[datetime, float]]:
    url = (f"{CG}/coins/{urllib.parse.quote(coin_id)}/market_chart/range?vs_currency=usd"
           f"&from={int(start.timestamp())}&to={int(end.timestamp())}")
    data = cached(url, fetch, ttl=120)
    out = []
    for row in (data or {}).get("prices", []) or []:
        try:
            out.append((datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc), float(row[1])))
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ football

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = [
    "eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "eng.2", "eng.fa", "eng.league_cup", "ned.1", "por.1", "usa.1", "ksa.1", "tur.1", "sco.1",
    "fifa.world", "fifa.worldq.caf", "caf.nations", "fifa.friendly", "uefa.nations",
]
TEAM_ALIASES = {
    "man u": "manchester united", "man utd": "manchester united", "manu": "manchester united", "united": "manchester united",
    "man city": "manchester city", "city": "manchester city", "spurs": "tottenham", "barca": "barcelona",
    "real": "real madrid", "madrid": "real madrid", "atleti": "atletico madrid", "psg": "paris saint-germain",
    "inter": "internazionale", "inter milan": "internazionale", "juve": "juventus", "bayern": "bayern munich",
    "dortmund": "borussia dortmund", "bvb": "borussia dortmund", "gunners": "arsenal", "the gunners": "arsenal",
    "reds": "liverpool", "pool": "liverpool", "blues": "chelsea", "villa": "aston villa", "toffees": "everton",
    "wolves": "wolverhampton", "forest": "nottingham forest", "newcastle": "newcastle united", "lfc": "liverpool",
    "super eagles": "nigeria", "naija": "nigeria", "leverkusen": "bayer leverkusen", "milan": "ac milan",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|afc|sc|the)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\s+", " ", s)).strip()


def team_matches(query: str, team: dict) -> bool:
    q = _norm(query)
    q = TEAM_ALIASES.get(q, q)
    names = {_norm(str(team.get(k, ""))) for k in ("displayName", "shortDisplayName", "name", "location")}
    abbr = str(team.get("abbreviation", "")).lower()
    if q == abbr or q in names:
        return True
    return any(q and n and (q in n.split() or (len(q) >= 4 and (q in n or n in q))) for n in names)


def _events(league: str, start: datetime, end: datetime, fetch: Fetch) -> list[dict]:
    dates = f"{start:%Y%m%d}-{end:%Y%m%d}"
    try:
        data = cached(f"{ESPN}/{league}/scoreboard?dates={dates}&limit=400", fetch, ttl=300)
    except Exception:
        return []
    evs = (data or {}).get("events", []) or []
    for e in evs:
        e["_league"] = league
    return evs


def _sides(event: dict) -> tuple[dict, dict]:
    comps = (event.get("competitions") or [{}])[0].get("competitors", [])
    home = next((c for c in comps if c.get("homeAway") == "home"), comps[0] if comps else {})
    away = next((c for c in comps if c.get("homeAway") == "away"), comps[1] if len(comps) > 1 else {})
    return home, away


def find_fixture(team_a: str, team_b: str, now: datetime, days: int = 14,
                 fetch: Fetch = http_fetch, leagues: Optional[list] = None) -> Optional[dict]:
    """Next fixture (not yet finished) between two teams, searched across major leagues."""
    best = None
    for lg in leagues or LEAGUES:
        for ev in _events(lg, now - timedelta(days=1), now + timedelta(days=days), fetch):
            home, away = _sides(ev)
            ht, at = home.get("team", {}), away.get("team", {})
            pair = ((team_matches(team_a, ht) and team_matches(team_b, at))
                    or (team_matches(team_a, at) and team_matches(team_b, ht)))
            if not pair:
                continue
            state = ev.get("status", {}).get("type", {}).get("state")
            if state == "post":
                continue
            kickoff = _parse_iso(ev.get("date"))
            if not kickoff:
                continue
            cand = {
                "event_id": str(ev.get("id")), "league": lg, "kickoff": kickoff.isoformat(),
                "home": ht.get("displayName"), "away": at.get("displayName"),
                "a_is_home": team_matches(team_a, ht) and team_matches(team_b, at),
                "name": f"{ht.get('displayName')} vs {at.get('displayName')}",
            }
            if best is None or kickoff < _parse_iso(best["kickoff"]):
                best = cand
        if best:
            return best  # leagues are ordered by popularity; stop at the first hit
    return best


def match_result(fixture: dict, fetch: Fetch = http_fetch) -> Optional[dict]:
    """Final score for a stored fixture, or None when not finished yet."""
    kickoff = _parse_iso(fixture["kickoff"])
    for ev in _events(fixture["league"], kickoff - timedelta(days=1), kickoff + timedelta(days=1), fetch):
        if str(ev.get("id")) != str(fixture["event_id"]):
            continue
        st = ev.get("status", {}).get("type", {})
        if not st.get("completed"):
            return {"final": False, "state": st.get("state"), "detail": st.get("detail")}
        home, away = _sides(ev)
        try:
            hs, as_ = int(float(home.get("score", 0))), int(float(away.get("score", 0)))
        except Exception:
            return None
        if st.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED"):
            return {"final": True, "void": True, "detail": st.get("detail")}
        return {"final": True, "home_score": hs, "away_score": as_, "detail": st.get("detail") or "FT",
                "home": home.get("team", {}).get("displayName"), "away": away.get("team", {}).get("displayName")}
    return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if re.match(r".*T\d{2}:\d{2}\+", s):
            s = s.replace("+", ":00+", 1)
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ------------------------------------------------------------------ Solana receipts

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")


def valid_address(a: str) -> bool:
    return bool(_B58.match(a or ""))


def valid_signature(s: str) -> bool:
    return bool(_SIG.match(s or ""))


def verify_solana_payment(rpc_url: str, signature: str, sender: str, receiver: str,
                          currency: str, amount: float, fetch: Fetch = http_fetch) -> dict:
    """Check a confirmed transaction moved at least ``amount`` from sender to receiver."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                   "commitment": "confirmed"}]}
    try:
        res = fetch(rpc_url, body)
    except Exception as exc:
        return {"ok": False, "reason": f"could not reach Solana RPC ({exc.__class__.__name__})"}
    tx = (res or {}).get("result")
    if not tx:
        return {"ok": False, "reason": "transaction not found (yet) — wait for confirmation and retry"}
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return {"ok": False, "reason": "that transaction failed on-chain"}
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in tx.get("transaction", {}).get("message", {}).get("accountKeys", [])]
    if currency == "SOL":
        try:
            s_i, r_i = keys.index(sender), keys.index(receiver)
        except ValueError:
            return {"ok": False, "reason": "sender or receiver wallet is not in that transaction"}
        received = (meta["postBalances"][r_i] - meta["preBalances"][r_i]) / 1e9
        sent = (meta["preBalances"][s_i] - meta["postBalances"][s_i]) / 1e9
        if received + 1e-9 >= amount and sent >= received - 1e-6:
            return {"ok": True, "received": round(received, 9)}
        return {"ok": False, "reason": f"receiver only got {received:.6f} SOL"}
    mint = {"USDC": USDC_MINT, "USDT": USDT_MINT}.get(currency)
    if not mint:
        return {"ok": False, "reason": f"{currency} cannot be verified on Solana"}

    def bal(rows, owner):
        return sum(float((r.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for r in rows or [] if r.get("mint") == mint and r.get("owner") == owner)
    received = bal(meta.get("postTokenBalances"), receiver) - bal(meta.get("preTokenBalances"), receiver)
    sent = bal(meta.get("preTokenBalances"), sender) - bal(meta.get("postTokenBalances"), sender)
    if received + 1e-6 >= amount and sent + 1e-6 >= amount:
        return {"ok": True, "received": round(received, 6)}
    return {"ok": False, "reason": f"receiver got {received:.2f} {currency} from that wallet, needed {amount:.2f}"}
