"""Turn chat text like "SOL above 300 by Friday 6pm stake 5 USDC" into a structured call.

Pure functions only (no network, no Hermes imports) so they are easy to test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_SHORT = {d[:3]: i for i, d in enumerate(WEEKDAYS)}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

CURRENCIES = {"usdc": "USDC", "usdt": "USDT", "sol": "SOL", "points": "PTS"}
FIAT = {"ngn", "naira", "₦", "$", "usd", "dollar", "dollars"}
# Currencies CALLIT can verify on Solana when someone posts a payment receipt.
ONCHAIN = {"SOL", "USDC", "USDT"}


@dataclass
class Stake:
    amount: float
    currency: str

    def label(self) -> str:
        amt = f"{self.amount:,.2f}".rstrip("0").rstrip(".")
        if self.currency == "NGN":
            return f"₦{amt}"
        if self.currency == "USD":
            return f"${amt}"
        return f"{amt} {self.currency}"


@dataclass
class ParsedCall:
    text: str                       # the prediction itself, cleaned
    kind: str = "custom"            # price | match | custom
    spec: dict = field(default_factory=dict)
    deadline: Optional[datetime] = None
    stake: Optional[Stake] = None
    judge: Optional[str] = None     # handle/name of a judge, if named
    errors: list = field(default_factory=list)


def _num(raw: str, suffix: str = "") -> float:
    val = float(raw.replace(",", ""))
    return val * {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)


# ---------------------------------------------------------------- stakes / judges

_STAKE_RE = re.compile(
    r"\b(?:stake|staking|bet)\s+(?P<pre>[$₦])?\s*(?P<amt>\d[\d,]*\.?\d*)\s*(?P<k>k)?\s*(?P<ccy>usdc|usdt|sol|ngn|naira|usd|dollars?|points)?\b",
    re.I,
)
_JUDGE_RE = re.compile(r"\bjudge(?:d)?\s*(?:by|is|:)?\s+@?(?P<who>[\w.]{2,40})", re.I)


def extract_stake(text: str) -> tuple[str, Optional[Stake]]:
    m = _STAKE_RE.search(text)
    if not m:
        return text, None
    ccy = (m.group("pre") or m.group("ccy") or "").lower()
    if not ccy or ccy in FIAT:
        currency = "FIAT"   # rejected in parse_call: CALLIT stakes are crypto only
    else:
        currency = CURRENCIES.get(ccy, ccy.upper())
    stake = Stake(_num(m.group("amt"), m.group("k") or ""), currency)
    return (text[: m.start()] + text[m.end():]).strip(" ,.;"), stake


def extract_judge(text: str) -> tuple[str, Optional[str]]:
    m = _JUDGE_RE.search(text)
    if not m:
        return text, None
    return (text[: m.start()] + text[m.end():]).strip(" ,.;"), m.group("who")


# ---------------------------------------------------------------- deadlines

_TIME_RE = re.compile(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?$", re.I)


def _apply_time(day: datetime, time_txt: Optional[str], default_end_of_day=True) -> Optional[datetime]:
    if not time_txt:
        return day.replace(hour=23, minute=59, second=0, microsecond=0) if default_end_of_day else day
    t = time_txt.strip().lower()
    if t in ("noon", "midday"):
        return day.replace(hour=12, minute=0, second=0, microsecond=0)
    if t == "midnight":
        return day.replace(hour=23, minute=59, second=0, microsecond=0)
    m = _TIME_RE.match(t)
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return day.replace(hour=h, minute=mi, second=0, microsecond=0)


def parse_when(txt: str, now: datetime, tz: str = "UTC") -> Optional[datetime]:
    """Parse a human deadline into an aware UTC datetime. Returns None when unparseable."""
    zone = ZoneInfo(tz) if tz else timezone.utc
    local_now = now.astimezone(zone)
    s = re.sub(r"\s+", " ", txt.strip().lower().rstrip(".!?"))
    s = re.sub(r"^(?:the )?", "", s)

    m = re.match(r"^(?:in|next|within|for)(?: the next)? (\d+(?:\.\d+)?) ?(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)$", s)
    if m:
        n, unit = float(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return (now + delta).astimezone(timezone.utc)

    # split off a trailing time ("friday 6pm", "tomorrow at 18:00")
    time_txt = None
    tm = re.search(r"(?:\s+at)?\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)$", s)
    if tm and not re.fullmatch(r"\d{1,2}", tm.group(1).strip()):
        time_txt, s = tm.group(1), s[: tm.start()].strip()
    elif re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight", s):
        time_txt, s = s, "today"

    day = None
    if s in ("today", "tonight", "eod", "end of day", "end of today"):
        day = local_now
    elif s == "tomorrow":
        day = local_now + timedelta(days=1)
    elif s in ("this weekend", "weekend", "the weekend", "end of week", "end of the week", "eow", "this week"):
        day = local_now + timedelta(days=(6 - local_now.weekday()) % 7)
    elif s in ("end of month", "end of the month", "eom", "this month"):
        nxt = (local_now.replace(day=28) + timedelta(days=4)).replace(day=1)
        day = nxt - timedelta(days=1)
    elif s in ("end of year", "end of the year", "eoy", "this year"):
        day = local_now.replace(month=12, day=31)
    else:
        wd = re.sub(r"^(?:this |next |on )", "", s)
        key = wd[:3]
        if wd in WEEKDAYS or (key in WEEKDAY_SHORT and len(wd) <= 4):
            target = WEEKDAY_SHORT[key]
            ahead = (target - local_now.weekday()) % 7
            if ahead == 0 and (s.startswith("next ") or local_now.hour >= 20):
                ahead = 7
            day = local_now + timedelta(days=ahead)
        else:
            iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2}))?", s)
            if iso:
                y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
                try:
                    day = local_now.replace(year=y, month=mo, day=d)
                except ValueError:
                    return None
                if iso.group(4):
                    time_txt = f"{iso.group(4)}:{iso.group(5)}"
            else:
                dm = re.match(r"^(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]{3})[a-z]*$", s) or None
                md = re.match(r"^([a-z]{3})[a-z]* (\d{1,2})(?:st|nd|rd|th)?$", s) or None
                mo_only = re.match(r"^(?:end of )?([a-z]{3})[a-z]*$", s)
                if dm and dm.group(2) in MONTHS:
                    d, mo = int(dm.group(1)), MONTHS[dm.group(2)]
                elif md and md.group(1) in MONTHS:
                    d, mo = int(md.group(2)), MONTHS[md.group(1)]
                elif mo_only and mo_only.group(1) in MONTHS:
                    mo = MONTHS[mo_only.group(1)]
                    first_next = datetime(local_now.year + (mo == 12), mo % 12 + 1, 1, tzinfo=zone)
                    d = (first_next - timedelta(days=1)).day
                else:
                    return None
                year = local_now.year
                try:
                    day = local_now.replace(year=year, month=mo, day=d)
                    if day.date() < local_now.date():
                        day = day.replace(year=year + 1)
                except ValueError:
                    return None
    if day is None:
        return None
    out = _apply_time(day, time_txt)
    if out is None:
        return None
    if s == "today" and time_txt and out < local_now:
        out += timedelta(days=1)
    return out.astimezone(timezone.utc)


_DEADLINE_RE = re.compile(
    r"\s+(?:by|before|on|until|till|at|in)\s+(?P<when>.+)$", re.I)
_BARE_DEADLINE_RE = re.compile(
    r"\s+(?P<when>(?:next|within|in the next) \d+(?:\.\d+)? ?(?:m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)|tonight|today|tomorrow|this weekend|end of (?:the )?(?:week|month|year)|next \w+)$", re.I)


def extract_deadline(text: str, now: datetime, tz: str) -> tuple[str, Optional[datetime]]:
    # try the longest trailing "by ..." first, then shorter tails
    for m in list(re.finditer(r"\s+(?:by|before|on|until|till|at|in)\s+", text, re.I)):
        tail = text[m.start():].strip()
        word, _, rest = tail.partition(" ")
        when_txt = rest if word.lower() != "in" else tail
        if word.lower() == "in" and rest.lower().startswith("the next "):
            when_txt = rest[4:]
        when = parse_when(when_txt, now, tz)
        mo = re.fullmatch(r"([a-z]{3})[a-z]*", rest.strip().lower())
        if when and word.lower() == "before" and mo and mo.group(1) in MONTHS:
            # "before December" means by the end of November, not the end of December
            start = parse_when(f"1 {mo.group(1)}", now, tz)
            if start:
                zone = ZoneInfo(tz) if tz else timezone.utc
                when = (start.astimezone(zone).replace(hour=0, minute=0) - timedelta(minutes=1)).astimezone(timezone.utc)
        if when:
            return text[: m.start()].strip(" ,.;"), when
    m = _BARE_DEADLINE_RE.search(text)
    if m:
        when = parse_when(m.group("when"), now, tz)
        if when:
            return text[: m.start()].strip(" ,.;"), when
    return text, None


# ---------------------------------------------------------------- what kind of call

_PRICE_RE = re.compile(
    r"^\$?(?P<sym>[a-z][a-z0-9]{1,11})\s+(?:price\s+)?(?:will\s+)?(?:be\s+|close\s+|go\s+|stay\s+)?"
    r"(?P<op>above|over|higher than|below|under|lower than|>=|<=|>|<|hits?|reach(?:es)?|touch(?:es)?|breaks?|tests?|flips?|dumps? to|pumps? to|drops? to|falls? to)"
    r"\s+\$?(?P<num>\d[\d,]*\.?\d*)\s*(?P<k>k|m)?\b", re.I)

_BEAT_RE = re.compile(
    r"^(?P<a>[\w .'&-]+?)\s+(?:will\s+|to\s+|gonna\s+|go\s+)?"
    r"(?:beat|beats|defeat|defeats|win against|wins against|win vs\.?|wins vs\.?|win over|wins over|trash|trashes|"
    r"destroy|destroys|thrash|thrashes|beat up|batter|batters|humble|humbles)\s+(?P<b>[\w .'&-]+?)$", re.I)
_DRAW_RE = re.compile(
    r"^(?P<a>[\w .'&-]+?)\s+(?:vs\.?|v|and|&|x)\s+(?P<b>[\w .'&-]+?)\s+(?:will\s+|to\s+|ends?\s+in\s+a\s+|is\s+a\s+)?(?:draw|tie|draws|ties)$",
    re.I)
_WIN_RE = re.compile(r"^(?P<a>[\w .'&-]+?)\s+(?:will\s+|to\s+)?(?:win|wins)\s+(?:vs\.?|v|against)\s+(?P<b>[\w .'&-]+?)$", re.I)


PRONOUNS = {"i", "you", "me", "u", "him", "her", "them", "we", "us", "my", "your", "he", "she", "they"}


def classify(text: str) -> tuple[str, dict]:
    t = text.strip()
    m = _PRICE_RE.match(t)
    if m:
        op = m.group("op").lower()
        if op in ("above", "over", "higher than", ">", ">="):
            spec = {"op": "above", "mode": "close"}
        elif op in ("below", "under", "lower than", "<", "<="):
            spec = {"op": "below", "mode": "close"}
        elif re.match(r"(dumps?|drops?|falls?) to", op):
            spec = {"op": "below", "mode": "touch"}
        elif re.match(r"pumps? to", op):
            spec = {"op": "above", "mode": "touch"}
        else:  # hits / reaches / breaks: direction decided against the live price at creation
            spec = {"op": "auto", "mode": "touch"}
        spec.update(symbol=m.group("sym").upper(), target=_num(m.group("num"), m.group("k") or ""))
        return "price", spec
    for rx, pick in ((_DRAW_RE, "draw"), (_BEAT_RE, "a"), (_WIN_RE, "a")):
        m = rx.match(t)
        if m:
            a, b = m.group("a").strip(), m.group("b").strip()
            words = {w.lower() for w in (a + " " + b).split()}
            if words & PRONOUNS or re.search(r"\b(in|at|on)\s+(ea|fc|fifa|cod|pubg|chess|ludo|whot|scrabble|2k|game)\b", b, re.I):
                break   # "I beat you in EA FC" is a game duel between people, not a football fixture
            if a and b and a.lower() != b.lower():
                return "match", {"team_a": a, "team_b": b, "pick": pick}
    return "custom", {}


def parse_call(raw: str, now: datetime, tz: str = "UTC") -> ParsedCall:
    text = re.sub(r"\s+", " ", raw).strip()
    text, stake = extract_stake(text)
    text, judge = extract_judge(text)
    text, deadline = extract_deadline(text, now, tz)
    text = text.strip(" ,.;")
    kind, spec = classify(text)
    pc = ParsedCall(text=text, kind=kind, spec=spec, deadline=deadline, stake=stake, judge=judge)
    if not text:
        pc.errors.append("empty prediction")
    if stake and stake.currency == "FIAT":
        pc.errors.append("stakes are crypto only. Use SOL, USDC or USDT, e.g. 'stake 0.1 SOL' or 'stake 5 USDC'")
    if stake and stake.amount <= 0:
        pc.errors.append("stake must be more than zero")
    if deadline and deadline <= now:
        pc.errors.append("that deadline is already in the past")
    return pc
