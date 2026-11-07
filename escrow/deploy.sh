#!/usr/bin/env bash
# Build + deploy the CALLIT duel escrow to Solana devnet and point the bot at it.
#   bash deploy          (from the callit folder)
# Safe to re-run: keys are kept in ~/.callit and reused.
set -e
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$1"; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="$HOME/.callit"; mkdir -p "$KEYS"
CLUSTER="${CALLIT_CLUSTER:-devnet}"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

if ! command -v cargo >/dev/null; then
  say "Installing Rust (about 1 minute)..."
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null
  . "$HOME/.cargo/env"
fi
if ! command -v solana >/dev/null; then
  say "Installing the Solana tools (about 2 minutes)..."
  sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)" >/dev/null
fi
say "$(solana --version)"

for k in deployer resolver program; do
  [ -f "$KEYS/$k.json" ] || solana-keygen new --no-bip39-passphrase --silent -o "$KEYS/$k.json" >/dev/null
done
DEPLOYER=$(solana-keygen pubkey "$KEYS/deployer.json")
RESOLVER=$(solana-keygen pubkey "$KEYS/resolver.json")
PROGRAM=$(solana-keygen pubkey "$KEYS/program.json")
solana config set -u "$CLUSTER" -k "$KEYS/deployer.json" >/dev/null

say "Funding the deployer on $CLUSTER: $DEPLOYER"
if [ "$CLUSTER" = "devnet" ]; then
  for i in 1 2 3; do
    bal=$(solana balance "$DEPLOYER" | awk '{print int($1)}')
    [ "$bal" -ge 3 ] && break
    solana airdrop 2 "$DEPLOYER" >/dev/null 2>&1 || sleep 8
  done
fi
bal=$(solana balance "$DEPLOYER" | awk '{print $1}')
echo "Deployer balance: $bal SOL"
if awk "BEGIN{exit !($bal < 2.5)}"; then
  say "Not enough SOL to deploy. Get free devnet SOL at https://faucet.solana.com for: $DEPLOYER  then run: bash deploy"
  exit 1
fi

say "Building the escrow..."
BUILD="$KEYS/build"; rm -rf "$BUILD"; cp -r "$HERE" "$BUILD"
(cd "$BUILD" && rm -rf tests && cargo build-sbf 2>&1 | tail -3)

say "Deploying program $PROGRAM ..."
solana program deploy "$BUILD/target/deploy/callit_escrow.so" --program-id "$KEYS/program.json" --keypair "$KEYS/deployer.json" -u "$CLUSTER"

say "Giving the referee a little SOL for transaction fees..."
solana transfer --allow-unfunded-recipient "$RESOLVER" 0.05 --keypair "$KEYS/deployer.json" -u "$CLUSTER" >/dev/null || true

say "Connecting the bot to the escrow..."
grep -q '^CALLIT_RESOLVER_KEY=' "$HOME/.hermes/.env" 2>/dev/null && sed -i '/^CALLIT_RESOLVER_KEY=/d' "$HOME/.hermes/.env"
printf 'CALLIT_RESOLVER_KEY=%s\n' "$(tr -d ' \n' < "$KEYS/resolver.json")" >> "$HOME/.hermes/.env"
hermes config set plugins.entries.callit.settings.escrow_program "$PROGRAM" >/dev/null
hermes config set plugins.entries.callit.settings.escrow_cluster "$CLUSTER" >/dev/null
hermes config set plugins.entries.callit.settings.bot_username "${CALLIT_BOT:-CallitHQBot}" >/dev/null

say "Done ✅  Escrow program: $PROGRAM  ($CLUSTER)"
echo "Explorer: https://solscan.io/account/$PROGRAM?cluster=$CLUSTER"
echo "Keys are in $KEYS (back them up, never share them). Now start the bot: bash go"
