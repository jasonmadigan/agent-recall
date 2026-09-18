#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CLI_ONLY=false
case "${1:-}" in
  --cli-only) CLI_ONLY=true ;;
  --help|-h) echo 'Usage: ./install.sh [--cli-only]'; exit 0 ;;
  '') ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

link() {
  local source="$1" target="$2"
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "Refusing to replace existing file or directory: $target" >&2
    return 1
  fi
  mkdir -p "$(dirname "$target")"
  ln -sfn "$source" "$target"
  echo "Installed $target"
}

link "$ROOT/agent_recall.py" "$BIN_DIR/agent-recall"
link "$ROOT/claude_recall.py" "$BIN_DIR/ccrecall"
if [[ "$CLI_ONLY" == false ]]; then
  link "$ROOT/skills/find-session" "$HOME/.agents/skills/find-session"
  link "$ROOT/skills/find-session" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/find-session"
  echo 'Restart your agent to discover the find-session skill.'
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo 'Add ~/.local/bin to PATH: export PATH="$HOME/.local/bin:$PATH"' ;;
esac
echo 'Try: agent-recall --help'
