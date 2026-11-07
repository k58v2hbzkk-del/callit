#!/usr/bin/env bash
# One-command setup for trying CALLIT in a GitHub Codespace (or any Linux box).
#   bash go        -> installs Hermes, connects your Telegram bot, installs CALLIT, starts the bot
# Safe to run again: it skips anything already done.
set -e
export PATH="$HOME/.local/bin:$PATH"
ENV_FILE="$HOME/.hermes/.env"

say() { printf "\n\033[1;35m==> %s\033[0m\n" "$1"; }

LOG=/tmp/callit-bot.log
# Already running (the codespace starts the bot by itself)? Just show its live log.
REPO="$(cd "$(dirname "$0")" && pwd)"
if pgrep -f "hermes gateway run" >/dev/null 2>&1; then
  BEFORE=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)
  git -C "$REPO" pull -q --ff-only 2>/dev/null || true
  if [ "$BEFORE" = "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" ]; then
    if [ "$1" = "--background" ]; then exit 0; fi
    say "CALLIT is already running. Showing the live log (control + C only hides it, the bot keeps going)."
    exec tail -n 60 -f "$LOG"
  fi
  say "New CALLIT version found - restarting the bot..."
  pkill -f "hermes gateway run" || true
  sleep 3
fi

# Always use the newest CALLIT code.
git -C "$REPO" pull -q --ff-only 2>/dev/null || true

if ! command -v hermes >/dev/null 2>&1; then
  say "Installing Hermes (2-4 minutes)..."
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive --skip-browser --skip-computer-use
  export PATH="$HOME/.local/bin:$PATH"
fi
say "Hermes: $(hermes --version 2>/dev/null | head -1)"

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

# Codespaces secrets (github.com/settings/codespaces) arrive as environment variables: use them if set.
if [ -n "$TELEGRAM_BOT_TOKEN" ] && ! grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE"; then
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOW_ALL_USERS=true\n' "$TELEGRAM_BOT_TOKEN" >> "$ENV_FILE"
  say "Bot token loaded from your Codespaces secret."
fi
if [ -n "$OPENROUTER_API_KEY" ] && ! grep -q '^OPENROUTER_API_KEY=' "$ENV_FILE"; then
  printf 'OPENROUTER_API_KEY=%s\n' "$OPENROUTER_API_KEY" >> "$ENV_FILE"
  hermes config set model.provider openrouter >/dev/null
  hermes config set model.default "${CALLIT_MODEL:-openrouter/free}" >/dev/null
  say "OpenRouter key loaded from your Codespaces secret."
fi

if ! grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE"; then
  say "Paste your Telegram bot token from @BotFather, then press return (it stays hidden):"
  read -rs TOKEN; echo
  if [ -z "$TOKEN" ]; then
    say "No token entered. Save it as a Codespaces secret named TELEGRAM_BOT_TOKEN, restart the codespace, then run: bash go.sh"
    exit 1
  fi
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOW_ALL_USERS=true\n' "$TOKEN" >> "$ENV_FILE"
  say "Bot token saved."
fi

if ! grep -q '^OPENROUTER_API_KEY=' "$ENV_FILE"; then
  say "Optional: paste an OpenRouter API key (lets people just talk to the bot). Press return to skip:"
  read -rs KEY; echo
  if [ -n "$KEY" ]; then
    printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >> "$ENV_FILE"
    hermes config set model.provider openrouter >/dev/null
    hermes config set model.default "${CALLIT_MODEL:-openrouter/free}" >/dev/null
    say "OpenRouter key saved."
  fi
fi

say "Preparing Telegram support..."
hermes pm install --extra telegram >/dev/null 2>&1 || true

say "Installing CALLIT..."
# A stuck earlier install (e.g. the automatic start) holds the plugin lock: clear it first.
for p in $(pgrep -f "hermes plugins (install|update)"); do kill "$p" 2>/dev/null || true; done
for p in $(pgrep -f "go --background"); do [ "$p" != "$$" ] && kill "$p" 2>/dev/null || true; done
sleep 1
if [ -d "$HOME/.hermes/plugins/callit" ]; then
  timeout 300 hermes plugins update callit </dev/null \
    || timeout 300 hermes plugins install k58v2hbzkk-del/callit --force --enable --yes-deps </dev/null || true
else
  timeout 300 hermes plugins install k58v2hbzkk-del/callit --force --enable --yes-deps </dev/null || true
fi
[ -d "$HOME/.hermes/plugins/callit" ] || { echo "CALLIT could not be installed - send a screenshot of this."; exit 1; }
hermes plugins enable callit >/dev/null 2>&1 || true
hermes config set plugins.entries.callit.settings.timezone "${CALLIT_TZ:-Africa/Lagos}" >/dev/null

# Duel escrow: give the bot its own referee key (only it can settle duels it refereed)
if ! grep -q '^CALLIT_RESOLVER_KEY=' "$ENV_FILE"; then
  say "Creating the bot's referee key for the duel escrow..."
  DER=$(mktemp); openssl genpkey -algorithm ed25519 -outform DER -out "$DER" 2>/dev/null
  KEYJSON=$(python3 - "$DER" <<'PY'
import json, subprocess, sys
der = open(sys.argv[1], "rb").read()
seed = der[-32:]
pub = subprocess.run(["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER", "-in", sys.argv[1]], capture_output=True).stdout[-32:]
print(json.dumps(list(seed + pub), separators=(",", ":")))
PY
)
  rm -f "$DER"
  printf 'CALLIT_RESOLVER_KEY=%s\n' "$KEYJSON" >> "$ENV_FILE"
fi
hermes config set plugins.entries.callit.settings.bot_username "${CALLIT_BOT:-CallitHQBot}" >/dev/null 2>&1 || true

# Safety: in Telegram the AI may only use CALLIT's own tools - never the shell, files or web.
# (Strangers can message a public bot; this keeps them away from the server and its keys.)
hermes config set platform_toolsets.telegram '["callit"]' >/dev/null 2>&1 || true
hermes config set platform_toolsets.discord '["callit"]' >/dev/null 2>&1 || true

# Public bot: strangers may only run /callit (and /help, /start) - never Hermes admin commands
# like /yolo, /update or /restart. Nobody is listed as admin ("0" is not a real Telegram user).
for scope in "" "group_"; do
  hermes config set "telegram.${scope}allow_admin_from" '["0"]' >/dev/null 2>&1 || true
  hermes config set "telegram.${scope}user_allowed_commands" '["callit","start"]' >/dev/null 2>&1 || true
done

say "Starting the bot. Go to your Telegram group and type:  /callit help"
say "(Keep this tab open. To stop: press control + C)"
if [ "$1" = "--background" ]; then
  setsid nohup hermes gateway run >> "$LOG" 2>&1 < /dev/null &
  say "CALLIT is running in the background (log: $LOG)."
  pkill -f "status-loop" >/dev/null 2>&1 || true
  setsid nohup bash "$REPO/status-loop" "$REPO" "$LOG" >/dev/null 2>&1 < /dev/null &
else
  exec hermes gateway run
fi
