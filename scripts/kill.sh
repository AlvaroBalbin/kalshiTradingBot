#!/bin/bash
# Emergency kill switch — stops the bot from placing any trades
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
touch "$SCRIPT_DIR/../KILL_SWITCH"
echo "Kill switch ACTIVATED. Bot will stop trading on next tick."
echo "To resume: bash scripts/resume.sh"
