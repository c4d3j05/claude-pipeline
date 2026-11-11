#!/usr/bin/env bash
# Install the dispatcher + nightly jobs for the current user:
#   macOS → launchd agents      Linux → systemd --user service + timer
set -euo pipefail

PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="$(command -v uv)" || { echo "uv not found on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_DIR/logs"

render() {  # render <src> <dst> <PATH value>
  sed -e "s#__PIPELINE_DIR__#$PIPELINE_DIR#g" -e "s#__UV__#$UV#g" -e "s#__PATH__#$3#g" "$1" > "$2"
  echo "wrote $2"
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  AGENTS="$HOME/Library/LaunchAgents"
  # Same PATH the daemon (and its worker subprocesses) need:
  # uv, ntree, gh, claude, git (Homebrew), poetry (pyenv shims), docker (/usr/local/bin).
  DAEMON_PATH="$HOME/.pyenv/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
  mkdir -p "$AGENTS"
  for name in dispatcher nightly; do
    dst="$AGENTS/ee.ignify.claude-pipeline.$name.plist"
    render "$PIPELINE_DIR/launchd/ee.ignify.claude-pipeline.$name.plist" "$dst" "$DAEMON_PATH"
    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
    echo "loaded ee.ignify.claude-pipeline.$name"
  done
  echo
  echo "Done. Tail the dispatcher with:"
  echo "  tail -f \"$PIPELINE_DIR/logs/dispatcher.err.log\""
  echo "Stop everything with:"
  echo "  launchctl unload \"$AGENTS/ee.ignify.claude-pipeline.dispatcher.plist\""
else
  UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  # Non-login systemd --user sessions have a minimal PATH; give it where uv, ntree, gh,
  # claude, git, poetry (pyenv) and docker usually live on Linux.
  DAEMON_PATH="$HOME/.pyenv/shims:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  mkdir -p "$UNITS"
  for unit in dispatcher.service nightly.service; do
    render "$PIPELINE_DIR/systemd/claude-pipeline-$unit" "$UNITS/claude-pipeline-$unit" "$DAEMON_PATH"
  done
  cp "$PIPELINE_DIR/systemd/claude-pipeline-nightly.timer" "$UNITS/"
  echo "wrote $UNITS/claude-pipeline-nightly.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now claude-pipeline-dispatcher.service claude-pipeline-nightly.timer
  echo
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    echo "NOTE: user services stop when you log out. To keep the dispatcher running:"
    echo "  sudo loginctl enable-linger $USER"
    echo
  fi
  echo "Done. Tail the dispatcher with:"
  echo "  journalctl --user -u claude-pipeline-dispatcher -f   # or tail -f \"$PIPELINE_DIR/logs/dispatcher.err.log\""
  echo "Stop everything with:"
  echo "  systemctl --user disable --now claude-pipeline-dispatcher.service claude-pipeline-nightly.timer"
fi
