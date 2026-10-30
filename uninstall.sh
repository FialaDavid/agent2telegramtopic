#!/usr/bin/env bash
# Agent2TelegramTopic one-command uninstaller - the mirror of install.sh.
# Usage:  curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/uninstall.sh | bash
# Stops the background service and removes config, state, the source clone and the launcher.
set -euo pipefail

# Recover the working directory if it was deleted out from under us.
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

PY="$(command -v python3 || command -v python || true)"

# The package's own `uninstall` does the heavy lifting. Find it in the source clone made by
# install.sh, or in the current directory when run from a clone.
run_uninstall() {
  [ -n "$PY" ] || return 1
  if [ -f ./agent2telegramtopic/__init__.py ]; then
    env "PYTHONPATH=$(pwd -P)" "$PY" -m agent2telegramtopic uninstall --yes; exit $?
  fi
  if [ -d "$HOME/.agent2telegramtopic-src/agent2telegramtopic" ]; then
    env "PYTHONPATH=$HOME/.agent2telegramtopic-src" "$PY" -m agent2telegramtopic uninstall --yes; exit $?
  fi
  return 1
}

if run_uninstall; then
  exit 0
fi

# Without the package we cannot safely identify its service or files.
say "Package not found. Nothing stopped and no files removed."
say "Re-run install.sh --no-setup, then run:  agent2telegramtopic uninstall --yes"
exit 1
