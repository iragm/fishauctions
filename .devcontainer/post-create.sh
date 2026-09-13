#!/usr/bin/env bash
# Sourced by postCreateCommand -- no `set -e` and no `exit`, or a failure here
# takes down the whole container create.

cd /workspace
pre-commit install-hooks

# --- Claude Code CLI in a real terminal -------------------------------------
# The VS Code extension launches the CLI over pipes (--input-format stream-json),
# so isInteractive() is false and "continue automatically at usage limit" can
# never arm. Running it from a pty is the only way to get that, which needs the
# binary on PATH and something that outlives the VS Code window.

devcontainer_setup_claude_cli() {
  local run=""
  [ "$(id -u)" = "0" ] || run="sudo"

  if ! command -v tmux >/dev/null 2>&1; then
    $run apt-get update -qq \
      && $run apt-get install -y --no-install-recommends tmux \
      || echo "post-create: tmux install failed, continuing" >&2
  fi

  # The wrapper lives on the /root/.claude volume, which outlives rebuilds.
  # It resolves the extension's bundled binary at run time because the
  # extension auto-updates and the versioned path moves underneath it.
  mkdir -p "${CLAUDE_CONFIG_DIR:-/root/.claude}/bin"
  cat > "${CLAUDE_CONFIG_DIR:-/root/.claude}/bin/claude" <<'WRAPEOF'
#!/usr/bin/env bash
set -euo pipefail

pick() {
  local d best="" a b
  for d in "$1"/anthropic.claude-code-*/resources/native-binary/claude; do
    [ -x "$d" ] || continue
    if [ -z "$best" ]; then best=$d; continue; fi
    a=$(printf '%s\n' "$best" | sed -E 's;.*claude-code-([0-9.]+)-.*;\1;')
    b=$(printf '%s\n' "$d"    | sed -E 's;.*claude-code-([0-9.]+)-.*;\1;')
    [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | tail -1)" = "$b" ] && best=$d
  done
  printf '%s' "$best"
}

BIN=$(pick "${HOME:-/root}/.vscode-server/extensions")
[ -n "$BIN" ] || BIN=$(pick /vscode/vscode-server/extensionsCache)

if [ -z "$BIN" ]; then
  echo "claude: no bundled Claude Code binary found -- is the VS Code extension installed?" >&2
  exit 127
fi

exec "$BIN" "$@"
WRAPEOF
  chmod +x "${CLAUDE_CONFIG_DIR:-/root/.claude}/bin/claude"

  local line='case ":$PATH:" in *":'"${CLAUDE_CONFIG_DIR:-/root/.claude}"'/bin:"*) ;; *) PATH="'"${CLAUDE_CONFIG_DIR:-/root/.claude}"'/bin:$PATH" ;; esac'
  local rc
  for rc in "$HOME/.bashrc" "$HOME/.profile"; do
    [ -f "$rc" ] || continue
    grep -q 'claude/bin' "$rc" || printf '\n# Claude Code CLI (bundled with the VS Code extension)\n%s\n' "$line" >> "$rc"
  done
}

devcontainer_setup_claude_cli
unset -f devcontainer_setup_claude_cli
