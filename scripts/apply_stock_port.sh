#!/bin/bash
# Take over a new stock omarchy.agents into the seppe.agents clone:
# port our patch set, validate, back up the live clone, install, mirror into
# the repo's plugin/, restart the shell and record the stock as handled.
# Run it yourself after the "Agents-plugin: update beschikbaar" notification.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
CLONE="$HOME/.config/omarchy/plugins/seppe.agents"
OUT=$(mktemp -d "${TMPDIR:-/tmp}/agents-port-XXXXXX")
trap 'rm -rf "$OUT"' EXIT

python3 "$REPO/scripts/port_from_stock.py" "$OUT"
cp "$REPO/plugin/manifest.json" "$OUT/"
cp "$REPO"/plugin/assets/{chatgpt,claude-cli}.svg "$OUT/assets/"
omarchy plugin validate "$OUT"

BACKUP="$HOME/.local/state/omarchy/agents/clone-backups/seppe.agents-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$(dirname "$BACKUP")"
cp -a "$CLONE" "$BACKUP"
echo "backup: $BACKUP"

cp "$OUT"/{Panel,Main,Agent}.qml "$OUT/README.md" "$CLONE/"
cp "$OUT"/assets/*.svg "$CLONE/assets/"
cp "$OUT"/{Panel,Main,Agent}.qml "$REPO/plugin/"
cp "$OUT"/assets/*.svg "$REPO/plugin/assets/"

omarchy-restart-shell >/dev/null 2>&1 || true
python3 "$REPO/scripts/check_stock_update.py" --no-notify --baseline
echo "done: clone updated; review and commit $REPO (git -C $REPO status)"
