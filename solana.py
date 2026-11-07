"""Tiny Solana client for the CALLIT escrow: addresses, reading bets, and the referee's payout transaction.

Pure Python + `cryptography` (already a Hermes core dependency). Layout matches escrow/src/lib.rs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSTEM = "11111111111111111111111111111111"
SPL_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MINTS = {
    "mainnet-beta": {"USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
    "devnet": {"USDC": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"},
}
DECIMALS = {"SOL": 9, "USDC": 6, "USDT": 6}
STATUS = {0: "open", 1: "active", 2: "done"}


# ---------------------------------------------------------------- base58

def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + full


def pk(s: str) -> bytes:
    b = b58decode(s)
    if len(b) != 32:
        raise ValueError(f"bad public key {s}")
    return b


# ---------------------------------------------------------------- PDAs (needs an ed25519 on-curve test)

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def find_pda(seeds: list[bytes], program: str) -> tuple[str, int]:
    prog = pk(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h), bump
    raise ValueError("no PDA")


def bet_address(program: str, bet_id: int) -> str:
    return find_pda([b"bet", struct.pack("<Q", bet_id)], program)[0]


def vault_address(program: str, bet: str) -> str:
    return find_pda([b"vault", pk(bet)], program)[0]


def ata(owner: str, mint: str) -> str:
    return find_pda([pk(owner), pk(SPL_PROGRAM), pk(mint)], ATA_PROGRAM)[0]


# ---------------------------------------------------------------- bet account

def decode_bet(data: bytes) -> dict:
    if len(data) < 157:
        raise ValueError("not a bet account")
    status, kind = data[0], data[1]
    bet_id, amount, deadline = struct.unpack_from("<QQq", data, 4)
    return {"status": STATUS.get(status, "?"), "kind": "spl" if kind == 1 else "sol", "bet_id": bet_id,
            "amount": amount, "deadline": deadline, "creator": b58encode(data[28:60]),
            "opponent": b58encode(data[60:92]), "mint": b58encode(data[92:124]), "winner": data[124],
            "resolver": b58encode(data[125:157])}


# ---------------------------------------------------------------- keys + transactions

def new_keypair_secret() -> str:
    """A fresh Solana keypair in solana-keygen's JSON format (64 bytes: seed + public key)."""
    from cryptography.hazmat.primitives import serialization
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return json.dumps(list(seed + pub), separators=(",", ":"))


def load_keypair(secret: str) -> Ed25519PrivateKey:
    secret = secret.strip()
    raw = bytes(json.loads(secret)) if secret.startswith("[") else b58decode(secret)
    if len(raw) not in (32, 64):
        raise ValueError("referee key must be a Solana keypair (64 bytes)")
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def pubkey_of(key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    return b58encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def _shortvec(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def build_tx(payer: Ed25519PrivateKey, program: str, metas: list[tuple[str, bool, bool]], data: bytes, blockhash: str) -> bytes:
    """Legacy transaction with one instruction, signed by the fee payer only."""
    payer_pk = pubkey_of(payer)
    flags: dict[str, list[bool]] = {payer_pk: [True, True]}
    order = [payer_pk]
    for key, signer, writable in metas + [(program, False, False)]:
        if key not in flags:
            flags[key] = [False, False]
            order.append(key)
        flags[key][0] |= signer
        flags[key][1] |= writable
    groups = ([k for k in order if flags[k][0] and flags[k][1]], [k for k in order if flags[k][0] and not flags[k][1]],
              [k for k in order if not flags[k][0] and flags[k][1]], [k for k in order if not flags[k][0] and not flags[k][1]])
    keys = [k for g in groups for k in g]
    if len(groups[0]) + len(groups[1]) != 1:
        raise ValueError("only the referee may sign")
    header = bytes([1, len(groups[1]), len(groups[3])])
    ix = bytes([keys.index(program)]) + _shortvec(len(metas)) + bytes(keys.index(k) for k, _, _ in metas) + _shortvec(len(data)) + data
    msg = header + _shortvec(len(keys)) + b"".join(pk(k) for k in keys) + pk(blockhash) + _shortvec(1) + ix
    return _shortvec(1) + payer.sign(msg) + msg


def settle_metas(program: str, bet_id: int, resolver: str, bet: dict) -> list[tuple[str, bool, bool]]:
    addr = bet_address(program, bet_id)
    metas = [(resolver, True, False), (addr, False, True), (bet["creator"], False, True),
             (bet["opponent"] if bet["status"] == "active" else bet["creator"], False, True)]
    if bet["kind"] == "spl":
        opp_ata = ata(bet["opponent"], bet["mint"]) if bet["status"] == "active" else ata(bet["creator"], bet["mint"])
        metas += [(vault_address(program, addr), False, True), (ata(bet["creator"], bet["mint"]), False, True),
                  (opp_ata, False, True), (SPL_PROGRAM, False, False)]
    return metas


# ---------------------------------------------------------------- RPC

Rpc = Callable[[str, Optional[dict]], Any]


class Escrow:
    def __init__(self, rpc_url: str, program: str, cluster: str, resolver_secret: Optional[str], fetch: Rpc):
        self.rpc_url, self.program, self.cluster, self.fetch = rpc_url, program, cluster, fetch
        self.key = load_keypair(resolver_secret) if resolver_secret else None
        self.resolver = pubkey_of(self.key) if self.key else None

    def _call(self, method: str, params: list) -> Any:
        res = self.fetch(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if res.get("error"):
            raise RuntimeError(res["error"].get("message", str(res["error"])))
        return res.get("result")

    def get_bet(self, bet_id: int) -> Optional[dict]:
        r = self._call("getAccountInfo", [bet_address(self.program, bet_id), {"encoding": "base64", "commitment": "confirmed"}])
        val = (r or {}).get("value")
        if not val:
            return None
        if val.get("owner") != self.program:
            return None
        data = base64.b64decode(val["data"][0])
        return decode_bet(data) if any(data) else None

    def settle(self, bet_id: int, outcome: int) -> str:
        """outcome: 0 = creator wins, 1 = opponent wins, 2 = refund. Returns the tx signature."""
        if not self.key:
            raise RuntimeError("referee key not configured")
        self.ensure_fee_money()
        bet = self.get_bet(bet_id)
        if not bet:
            raise RuntimeError("no locked stake found for this duel")
        if bet["status"] == "open" and outcome != 2:
            outcome = 2  # nobody joined: only a refund is possible
        bh = self._call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        tx = build_tx(self.key, self.program, settle_metas(self.program, bet_id, self.resolver, bet), bytes([2, outcome]), bh)
        return self._call("sendTransaction", [base64.b64encode(tx).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}])

    def ensure_fee_money(self) -> None:
        """On devnet, top up the referee with free test SOL so it can pay transaction fees."""
        if self.cluster != "devnet" or not self.resolver:
            return
        try:
            lamports = self._call("getBalance", [self.resolver, {"commitment": "confirmed"}])["value"]
            if lamports < 20_000_000:
                self._call("requestAirdrop", [self.resolver, 1_000_000_000])
        except Exception:
            pass

    def explorer(self, sig: str) -> str:
        return f"https://solscan.io/tx/{sig}" + ("?cluster=devnet" if self.cluster == "devnet" else "")
