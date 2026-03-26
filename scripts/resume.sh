#!/bin/bash
# Remove kill switch — allows bot to resume trading
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "$SCRIPT_DIR/../KILL_SWITCH"
echo "Kill switch REMOVED. Bot will resume trading on next tick."
