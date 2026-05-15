#!/usr/bin/env bash
set -euo pipefail

# Load Anthropic API key from secrets file
if [ ! -f "$HOME/.config/trooth/secrets.env" ]; then
    echo "ERROR: ~/.config/trooth/secrets.env not found. Cannot start bot without Anthropic API key." >&2
    exit 1
fi
set -a
source "$HOME/.config/trooth/secrets.env"
set +a

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not loaded from secrets.env" >&2
    exit 1
fi

# Belt-and-suspenders: explicitly disable live trading at env-var level
export LIVE_TRADING=false

# Activate venv and launch in console mode
cd "$(dirname "$0")/.."
source .venv/bin/activate
cd python
exec python main.py --console
