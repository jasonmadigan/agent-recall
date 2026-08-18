#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SKILLS_DIR="${HOME}/.claude/skills"

mkdir -p "$BIN_DIR" "$SKILLS_DIR"

ln -sfn "$ROOT/claude_recall.py" "$BIN_DIR/ccrecall"
chmod +x "$ROOT/claude_recall.py"
ln -sfn "$ROOT/skills/find-session" "$SKILLS_DIR/find-session"

if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
  echo "Note: $BIN_DIR is not on PATH. Add it, e.g.:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "Installed ccrecall -> $BIN_DIR/ccrecall"
echo "Installed skill    -> $SKILLS_DIR/find-session"
echo "Restart Claude Code (or /reload) so /find-session is available."
echo "Try: ccrecall --help"
