# 🔮 CALLIT

**Turn any group chat into a prediction league, with Hermes as the referee.**

Every group chat has that friend who says "mark my words" and then disappears when they're wrong. CALLIT keeps the receipts.

Someone makes a call. Friends back it or fade it. Hermes locks the sides, checks the real result itself (live prices, football scores, or a group vote), scores everyone, and keeps a season leaderboard. You can put money on it if you want, and payments made in crypto get verified on-chain.

```
/callit Arsenal beat Chelsea stake 5 USDC
```
```
📣 CALLIT #1 by Taj
“Arsenal beat Chelsea”
Arsenal vs Chelsea kicks off Sun 08 Nov, 17:30 WAT.
⏳ Deadline: Sun 08 Nov, 19:20 WAT
🔒 Sides close: Sun 08 Nov, 17:30 WAT
💰 Stake: 5 USDC each side
⚖️ I'll check the final score myself and settle it.

Agree? /callit with 1   ·   Think it's cap? /callit against 1
```
Two hours later, nobody had to ask:
```
🏁 CALLIT #1 · ✅ CALLED IT
“Arsenal beat Chelsea” by Taj
📊 Final: Arsenal 2-1 Chelsea.
🏆 Bayo +20, Taj +25
💀 Ada -5, Truth -5
💸 #1: Ada → Bayo 5 USDC
💸 #2: Truth → Taj 5 USDC
Pay, then post the receipt: /callit paid <debt#> <tx signature>
```

## Why a plugin and not a prompt

A prompt can't hold a group to its word. CALLIT has things a prompt or a skill can't do:

- **A memory that belongs to the group.** Every call, side, vote and payment lives in a per-chat league (SQLite in Hermes' plugin data). It survives restarts, context compression and model switches. A season builds up like the chat's own history.
- **It works while nobody is talking.** `/callit referee on` schedules a Hermes cron job that checks results every 15 minutes and posts only when something settles (it replies `[SILENT]` otherwise). It runs on the `callit` toolset only, so each check costs almost nothing.
- **Deterministic settlement.** Results come from live data, never from the model's opinion. Prices come from CoinGecko, football scores from ESPN, and custom calls from a named judge or a weighted group vote.
- **Identity you can't fake.** Who made a call or took a side comes from Hermes' gateway routing (`source.user_id`), not from what the model types. Nobody can join a call "as" someone else.
- **Rules against gaming it:**
  - Sides close at kickoff for matches, or after 25% of the window for everything else, so nobody can wait until the answer is obvious.
  - No switching sides, and you can't fade your own call.
  - One receipt can only pay one debt.
  - In votes, people with nothing on the call count double.
  - Calls with no data are voided and every stake is returned.

## Works everywhere Hermes lives

It's one `/callit` command on Telegram, Discord, Slack, WhatsApp, Signal, the CLI and Desktop. Each chat (and each Telegram topic or Discord thread) gets its own league.

You can also just talk. The agent gets five tools, so "I bet SOL flips 300 before Friday, 5 USDC" becomes a locked call without anyone typing a command.

## Commands

| Command | What it does |
|---|---|
| `/callit <prediction> by <when> [stake 5 USDC] [judge @name]` | Make a call |
| `/callit with <id>` / `/callit against <id>` | Back it or fade it |
| `/callit board` | Open calls |
| `/callit table` | Season leaderboard, streaks 🔥, Prophet of the season |
| `/callit shame` | Hall of Shame: worst calls, coldest players, unpaid debts |
| `/callit me` / `/callit stats @name` | Record, accuracy, last 5 results |
| `/callit show <id>` / `/callit receipts <id>` | One call, and its full audit trail |
| `/callit vote <id> yes\|no` | Settle a custom call |
| `/callit referee on\|off` / `/callit settle` | Auto-referee, or check results now |
| `/callit wallet <solana address>` | Where you get paid |
| `/callit paid <debt#> <tx>` | Post a payment receipt; checked on Solana |
| `/callit received <debt#>` | Confirm a cash or bank payment |
| `/callit season new [name]` | Crown a champion and reset the table |

**Call types it understands**

- **Crypto:** `SOL above 300 by Friday 6pm`, `BTC hits 100k by end of month` (settles early the moment it touches), `ETH below 2500 in 3 days`. Coins resolve by market cap, so a copycat ticker never wins.
- **Football:** `Arsenal beat Chelsea`, `Real Madrid vs Barcelona draw`. CALLIT finds the fixture in the next 14 days across 20+ competitions (Premier League, La Liga, Serie A, Bundesliga, Ligue 1, UCL, UEL, AFCON, World Cup qualifiers and more). It closes sides at kickoff and settles on the final score. Nicknames like "Man U", "Barca", "Spurs" and "Super Eagles" work.
- **Anything else:** `Davido drops an album before December judge @truth` is settled by the judge, or by a group vote when no judge is named.

**Duels:** `/callit duel @friend <bet>` challenges one person. They `accept`, then after the game each side reports `won` or `lost`. A concession settles it at once; if both claim the win, the group votes. Anyone who doesn't accept in time lands in the Hall of Shame 🐔. Works for prices, football, video games (EA FC, COD…) and real life.

**Pools:** `/callit pool <question>` lets everyone guess a number or score; the closest 3 win (+30/+20/+10, stakes split 50/30/20). Price and score pools settle themselves; anything else has a judge enter the answer.

**Scoring:** a correct call earns 10 points plus 5 for each person on the losing side. The caller gets +5 when their own call hits, and streaks add up to +10 more. A wrong call costs 5 points.

## Money (optional, crypto only)

Stakes are **SOL, USDC or USDT on Solana**. Base and BNB are on the roadmap. Fiat stakes are refused.

**Duels use an on-chain escrow** (`escrow/`, a small Solana program):
1. The challenger taps the bot's link and locks their stake from Phantom or Solflare (the stake page lives on the `gh-pages` branch).
2. The opponent accepts in chat, then locks the same amount. Only now is the duel live.
3. The result comes from a concession, the football API, the price feed, a judge or a group vote. CALLIT's referee key then tells the contract who won, and the contract pays them the whole pot. No fee.

The contract can **only pay one of the two players**:
- there is no admin withdraw and no third-party payout
- if the referee disappears, anyone can refund both players 7 days after the deadline
- a player can concede on-chain without the bot
- payouts that fail are retried automatically

**Group calls and pools** use a ledger: CALLIT nets who owes whom, and Solana payment receipts are checked on-chain (`/callit wallet`, `/callit paid`).

**Run the escrow yourself:** `bash deploy` installs the Solana tools, makes a referee key, and builds and deploys the program to **devnet**. It then points the bot at the program. The program is **unaudited**, so get an audit before using real money on mainnet. Real-money wagering is regulated in many places, so check your local rules.

## Install

```bash
hermes plugins install callit
hermes plugins enable callit
```
Then type `/callit help` in any chat Hermes is in. To see the gameplay on one device, use hotseat mode, which works locally only: `/callit as Truth against 1`.

Settings (Desktop → Settings → Plugins → callit, or `plugins.entries.callit.settings`):

| Key | Default | |
|---|---|---|
| `timezone` | `UTC` | How deadlines like "Friday 6pm" are read (e.g. `Africa/Lagos`) |
| `min_votes` | `3` | Votes needed to settle a custom call without a judge |
| `referee_every` | `15m` | Auto-referee interval |
| `solana_rpc_url` | public mainnet RPC | Used only to verify receipts |

## Disclosures

- **Network calls (read-only, no keys, no personal data sent):**
  - `api.coingecko.com`: prices for price calls
  - `site.api.espn.com`: football fixtures and scores for match calls
  - the configured Solana RPC: only when someone runs `/callit paid`
- **Escrow (only when configured):** the referee key is kept as the Hermes secret `CALLIT_RESOLVER_KEY`. It can sign one thing: telling the escrow contract which of the two players won. CALLIT reads bet accounts and sends those settle transactions to the configured Solana RPC.
- **Chat safety:** `go.sh` limits the AI in Telegram and Discord to CALLIT's own tools (no shell, files or web), so strangers messaging a public bot can't reach the server.
- **Storage:** `<HERMES_HOME>/plugin-data/callit/data.db` holds calls, sides, votes, points, debts, Solana *public* addresses and an audit log. There are no secrets and no private keys.
- **Background work:** only when a user runs `/callit referee on`. That creates one visible Hermes cron job through `cronjob_manage`, which `/callit referee off` removes. No shell commands, no child processes, no reads outside plugin data.
- **No telemetry.**

## Development

```bash
python -m pytest -q tests      # run from the folder that contains callit/ — offline, fake feeds
hermes plugins validate . --install-deps
```

Built by [Tajudeen](https://x.com/Tajudeen) for the Hermes Agent Plugin Hackathon. MIT licensed.
