#!/usr/bin/env bash
# Install the dispatcher + nightly launchd agents for the current user.
set -euo pipefail

PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="$(command -v uv)"
AGENTS="$HOME/Library/LaunchAgents"
# Give launchd the same PATH the daemon needs (uv, ntree, gh, claude, git).
DAEMON_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

mkdir -p "$AGENTS" "$PIPELINE_DIR/logs"

for name in dispatcher nightly; do
  src="$PIPELINE_DIR/launchd/ee.ignify.claude-pipeline.$name.plist"
  dst="$AGENTS/ee.ignify.claude-pipeline.$name.plist"
  sed -e "s#__PIPELINE_DIR__#$PIPELINE_DIR#g" \
      -e "s#__UV__#$UV#g" \
      -e "s#__PATH__#$DAEMON_PATH#g" \
      "$src" > "$dst"
  echo "wrote $dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "loaded ee.ignify.claude-pipeline.$name"
done

echo
echo "Done. Tail the dispatcher with:"
echo "  tail -f \"$PIPELINE_DIR/logs/dispatcher.err.log\""
echo "Stop everything with:"
echo "  launchctl unload \"$AGENTS/ee.ignify.claude-pipeline.dispatcher.plist\""
