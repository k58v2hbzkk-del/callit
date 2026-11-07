// Instruction builders for the CALLIT escrow program. Layout must match escrow/src/lib.rs.
import { PublicKey, SystemProgram, TransactionInstruction } from "@solana/web3.js";

export const TOKEN = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
export const ATA_PROGRAM = new PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
export const MINTS = {
  "mainnet-beta": { USDC: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", USDT: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB" },
  devnet: { USDC: "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU" },
};
export const DECIMALS = { SOL: 9, USDC: 6, USDT: 6 };
const enc = new TextEncoder();

export function u64(n) {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(n), true);
  return b;
}
export function i64(n) {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigInt64(0, BigInt(n), true);
  return b;
}
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((s, p) => s + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
};

export const betAddress = (pid, betId) => PublicKey.findProgramAddressSync([enc.encode("bet"), u64(betId)], pid)[0];
export const vaultAddress = (pid, bet) => PublicKey.findProgramAddressSync([enc.encode("vault"), bet.toBytes()], pid)[0];
export const ata = (owner, mint) => PublicKey.findProgramAddressSync([owner.toBytes(), TOKEN.toBytes(), mint.toBytes()], ATA_PROGRAM)[0];

const m = (pubkey, isSigner, isWritable) => ({ pubkey, isSigner, isWritable });

export function createIx(pid, creator, betId, amount, deadline, opponent, mint) {
  const bet = betAddress(pid, betId);
  const opp = opponent ? new PublicKey(opponent).toBytes() : new Uint8Array(32);
  const keys = [m(creator, true, true), m(bet, false, true), m(SystemProgram.programId, false, false)];
  if (mint) keys.push(m(mint, false, false), m(ata(creator, mint), false, true), m(vaultAddress(pid, bet), false, true), m(TOKEN, false, false));
  const data = cat(Uint8Array.of(0), u64(betId), u64(amount), i64(deadline), opp);
  return new TransactionInstruction({ programId: pid, keys, data });
}

export function joinIx(pid, joiner, betId, mint) {
  const bet = betAddress(pid, betId);
  const keys = [m(joiner, true, true), m(bet, false, true), m(SystemProgram.programId, false, false)];
  if (mint) keys.push(m(ata(joiner, mint), false, true), m(vaultAddress(pid, bet), false, true), m(TOKEN, false, false));
  return new TransactionInstruction({ programId: pid, keys, data: Uint8Array.of(1) });
}

export function cancelIx(pid, creator, betId, mint) {
  const bet = betAddress(pid, betId);
  const keys = [m(creator, true, true), m(bet, false, true)];
  if (mint) keys.push(m(vaultAddress(pid, bet), false, true), m(ata(creator, mint), false, true), m(TOKEN, false, false));
  return new TransactionInstruction({ programId: pid, keys, data: Uint8Array.of(4) });
}

export function concedeIx(pid, player, betId, creator, opponent, mint) {
  const bet = betAddress(pid, betId);
  const keys = [m(player, true, false), m(bet, false, true), m(creator, false, true), m(opponent, false, true)];
  if (mint) keys.push(m(vaultAddress(pid, bet), false, true), m(ata(creator, mint), false, true), m(ata(opponent, mint), false, true), m(TOKEN, false, false));
  return new TransactionInstruction({ programId: pid, keys, data: Uint8Array.of(3) });
}

/** Decode the 128-byte bet account (see Bet::pack in lib.rs). */
export function decodeBet(data) {
  const d = data instanceof Uint8Array ? data : new Uint8Array(data);
  const v = new DataView(d.buffer, d.byteOffset, d.byteLength);
  const pk = (a) => new PublicKey(d.slice(a, a + 32));
  return {
    status: d[0], kind: d[1], betId: v.getBigUint64(4, true), amount: v.getBigUint64(12, true),
    deadline: Number(v.getBigInt64(20, true)), creator: pk(28), opponent: pk(60), mint: pk(92), winner: d[124],
  };
}

export function toUnits(amountStr, token) {
  const [w, f = ""] = String(amountStr).split(".");
  const dec = DECIMALS[token];
  return BigInt(w || "0") * 10n ** BigInt(dec) + BigInt((f + "0".repeat(dec)).slice(0, dec) || "0");
}
