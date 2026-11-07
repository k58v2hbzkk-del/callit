#!/usr/bin/env bash
# One-command setup for trying CALLIT in a GitHub Codespace (or any Linux box).
#   bash go.sh        -> installs Hermes, connects your Telegram bot, installs CALLIT, starts the bot
# Safe to run again: it skips anything already done.
set -e
export PATH="$HOME/.local/bin:$PATH"
ENV_FILE="$HOME/.hermes/.env"

say() { printf "\n\033[1;35m==> %s\033[0m\n" "$1"; }

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
if ! hermes plugins list 2>/dev/null | grep -q callit; then
  hermes plugins install k58v2hbzkk-del/callit --enable --yes-deps
else
  hermes plugins update callit </dev/null >/dev/null 2>&1 || hermes plugins install k58v2hbzkk-del/callit --force --enable --yes-deps
fi
hermes plugins enable callit >/dev/null 2>&1 || true
hermes config set plugins.entries.callit.settings.timezone "${CALLIT_TZ:-Africa/Lagos}" >/dev/null

say "Starting the bot. Go to your Telegram group and type:  /callit help"
say "(Keep this tab open. To stop: press control + C)"
exec hermes gateway run
