import { Buffer } from "buffer";
globalThis.Buffer = globalThis.Buffer || Buffer;
import { Connection, PublicKey, Transaction, LAMPORTS_PER_SOL } from "@solana/web3.js";
import { MINTS, ata, betAddress, cancelIx, concedeIx, createIx, decodeBet, joinIx, toUnits } from "./escrow.js";

const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
const P = {
  action: q.get("a") || "create",
  betId: q.get("id"),
  no: q.get("n") || "",
  amount: q.get("amt") || "0",
  token: (q.get("tok") || "SOL").toUpperCase(),
  deadline: Number(q.get("dl") || 0),
  opponent: q.get("opp") || "",
  net: q.get("net") === "mainnet-beta" ? "mainnet-beta" : "devnet",
  pid: q.get("pid"),
  title: q.get("t") || "",
  bot: (q.get("bot") || "").replace(/^@/, ""),
  vs: q.get("vs") || "",
};
const RPC = q.get("rpc") || (P.net === "devnet" ? "https://api.devnet.solana.com" : "https://api.mainnet-beta.solana.com");
const conn = new Connection(RPC, "confirmed");
const explorer = (sig) => `https://solscan.io/tx/${sig}${P.net === "devnet" ? "?cluster=devnet" : ""}`;
const short = (k) => k.slice(0, 4) + "…" + k.slice(-4);

let wallet = null;
let me = null;

function provider() {
  const w = window;
  return w.phantom?.solana || w.solflare || w.backpack?.solana || (w.solana?.isPhantom || w.solana ? w.solana : null);
}

function status(msg, kind = "") {
  const el = $("status");
  el.className = "status " + kind;
  el.innerHTML = msg;
}

function render() {
  const verb = { create: "Lock your stake", join: "Accept & lock your stake", cancel: "Cancel & get your stake back", concede: "Concede (opponent gets the pot)" }[P.action];
  $("kicker").textContent = P.no ? `DUEL #${P.no}` : "CALLIT DUEL";
  $("title").textContent = P.title ? `“${P.title}”` : "";
  $("vs").textContent = P.vs;
  $("stake").textContent = `${P.amount} ${P.token}`;
  $("stakeNote").textContent = P.action === "join" ? "your side · winner takes both" : "each side · winner takes both";
  $("go").textContent = verb || "Continue";
  $("net").textContent = P.net === "devnet" ? "DEVNET · test coins" : "MAINNET";
  $("net").className = "pill " + (P.net === "devnet" ? "dev" : "main");
  if (P.deadline) $("deadline").textContent = "Deadline " + new Date(P.deadline * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  if (P.bot) { $("back").href = `https://t.me/${P.bot}`; }
  if (!P.pid || !P.betId) return status("This link is incomplete. Ask the bot for a new one.", "bad");
  if (!provider()) {
    const here = encodeURIComponent(location.href);
    const ref = encodeURIComponent(location.origin);
    $("wallets").innerHTML = `
      <a class="btn alt" href="https://phantom.app/ul/browse/${here}?ref=${ref}">Open in Phantom</a>
      <a class="btn alt" href="https://solflare.com/ul/v1/browse/${here}?ref=${ref}">Open in Solflare</a>`;
    $("go").style.display = "none";
    status("Open this page inside your wallet app to continue.");
  }
}

async function connect() {
  const p = provider();
  if (!p) return null;
  const res = await p.connect();
  const key = res?.publicKey || p.publicKey;
  wallet = p;
  me = new PublicKey(key.toString());
  $("who").textContent = "Connected · " + short(me.toBase58());
  return me;
}

async function preflight(pid, mint, units) {
  const bet = betAddress(pid, BigInt(P.betId));
  const info = await conn.getAccountInfo(bet);
  const state = info && info.data.length >= 125 ? decodeBet(info.data) : null;
  if (P.action === "create" && state) throw new Error(state.creator.equals(me) ? "Your stake is already locked ✅" : "This duel id is already taken. Ask the bot for a new link.");
  if (P.action === "join") {
    if (!state) throw new Error("Your opponent hasn't locked their stake yet. Try again once they have.");
    if (state.status !== 0) throw new Error("This duel already has two players.");
    if (state.creator.equals(me)) throw new Error("That's your own duel. Send this link to your opponent.");
  }
  if (P.action === "cancel" && (!state || state.status !== 0)) throw new Error("Nothing to cancel.");
  if (P.action === "concede" && (!state || state.status !== 1)) throw new Error("This duel isn't live.");
  if (P.action === "create" || P.action === "join") {
    if (!mint) {
      const bal = BigInt(await conn.getBalance(me));
      if (bal < units + 5_000_000n) throw new Error(`Not enough SOL. You have ${(Number(bal) / LAMPORTS_PER_SOL).toFixed(4)} SOL.` + faucet());
    } else {
      let have = 0n;
      try { have = BigInt((await conn.getTokenAccountBalance(ata(me, mint))).value.amount); } catch { /* no account */ }
      if (have < units) throw new Error(`Not enough ${P.token} in this wallet.` + faucet());
    }
  }
  return state;
}

function faucet() {
  if (P.net !== "devnet") return "";
  return P.token === "SOL"
    ? ` Get free test SOL at <a href="https://faucet.solana.com" target="_blank">faucet.solana.com</a>.`
    : ` Get free test USDC at <a href="https://faucet.circle.com" target="_blank">faucet.circle.com</a> (Solana Devnet).`;
}

async function go() {
  $("go").disabled = true;
  try {
    status("Connecting wallet…");
    if (!me) await connect();
    const pid = new PublicKey(P.pid);
    const mintStr = P.token === "SOL" ? null : MINTS[P.net]?.[P.token];
    if (P.token !== "SOL" && !mintStr) throw new Error(`${P.token} isn't available on ${P.net} yet.`);
    const mint = mintStr ? new PublicKey(mintStr) : null;
    const units = P.token === "SOL" ? toUnits(P.amount, "SOL") : toUnits(P.amount, P.token);
    status("Checking the duel…");
    const state = await preflight(pid, mint, units);
    let ix;
    if (P.action === "create") ix = createIx(pid, me, BigInt(P.betId), units, P.deadline, P.opponent || null, mint);
    else if (P.action === "join") ix = joinIx(pid, me, BigInt(P.betId), mint);
    else if (P.action === "cancel") ix = cancelIx(pid, me, BigInt(P.betId), mint);
    else ix = concedeIx(pid, me, BigInt(P.betId), state.creator, state.opponent, mint);
    const tx = new Transaction().add(ix);
    tx.feePayer = me;
    const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
    tx.recentBlockhash = blockhash;
    status("Approve it in your wallet…");
    let sig;
    if (wallet.signAndSendTransaction) {
      const r = await wallet.signAndSendTransaction(tx);
      sig = r.signature || r;
    } else {
      const signed = await wallet.signTransaction(tx);
      sig = await conn.sendRawTransaction(signed.serialize());
    }
    status("Confirming on Solana…");
    const conf = await conn.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight }, "confirmed");
    if (conf.value.err) throw new Error("The transaction failed on-chain.");
    const done = {
      create: "🔒 Stake locked! Now your opponent accepts in the group.",
      join: "⚔️ Duel is ON. Both stakes are locked in the contract.",
      cancel: "↩️ Cancelled. Your stake is back in your wallet.",
      concede: "🫡 Conceded. The pot went to your opponent.",
    }[P.action];
    status(`${done}<br><a href="${explorer(sig)}" target="_blank">View on Solscan ↗</a>`, "good");
    $("go").style.display = "none";
    $("back").style.display = "flex";
  } catch (e) {
    const msg = e?.message || String(e);
    status(/reject|denied|cancel/i.test(msg) ? "You cancelled in the wallet. Nothing was sent." : msg, "bad");
    $("go").disabled = false;
  }
}

window.addEventListener("load", () => {
  render();
  $("go").addEventListener("click", go);
  const p = provider();
  if (p?.isConnected && p.publicKey) { wallet = p; me = new PublicKey(p.publicKey.toString()); $("who").textContent = "Connected · " + short(me.toBase58()); }
});
