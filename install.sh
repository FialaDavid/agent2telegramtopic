#!/usr/bin/env bash
# Agent2TelegramTopic one-command installer (macOS and Linux).
# Usage:  curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/install.sh | bash
#         (or run ./install.sh from a clone; add --no-setup to skip the setup wizard)
# It checks python3 / tmux / the agent CLI, fetches the source, drops an `agent2telegramtopic`
# launcher on your PATH and starts the setup wizard. Nothing needs sudo.
# Parse everything before an update can replace the script currently being read.
main() {
set -euo pipefail
NO_SETUP=0
for arg in "$@"; do
  case "$arg" in
    --no-setup) NO_SETUP=1 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# Recover the working directory if it was deleted (e.g. you just uninstalled while sitting in
# the source clone) - otherwise git/curl fail with "cannot access parent directories: getcwd".
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

REPO="https://github.com/FialaDavid/agent2telegramtopic.git"
TARBALL="https://codeload.github.com/FialaDavid/agent2telegramtopic/tar.gz/refs/heads/main"
NEED_PY_MAJOR=3
NEED_PY_MINOR=11

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) err "Only macOS and Linux are supported." ;;
esac

# Pick where to drop the launcher. Prefer a directory ALREADY on PATH and writable - then the
# command works in the *current* terminal immediately (a piped installer cannot change the parent
# shell's PATH). Fall back to ~/.local/bin (added to PATH via the rc files for future shells).
pick_bindir() {
  oldifs="$IFS"; IFS=:
  for d in $PATH; do
    case "$d" in
      "$HOME"/*) if [ -d "$d" ] && [ -w "$d" ]; then IFS="$oldifs"; printf '%s' "$d"; return; fi ;;
    esac
  done
  IFS="$oldifs"; printf '%s' "$HOME/.local/bin"
}

# 0) Preflight: collect EVERYTHING missing up front and give one install command.
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
command -v tmux    >/dev/null 2>&1 || need="$need tmux"      # every topic is a tmux session
if [ -n "$need" ]; then
  need="${need# }"
  if command -v apt-get >/dev/null 2>&1; then
    err "Missing: $need - install with:  sudo apt-get update && sudo apt-get install -y $need"
  elif command -v dnf >/dev/null 2>&1; then
    err "Missing: $need - install with:  sudo dnf install -y $need"
  elif command -v pacman >/dev/null 2>&1; then
    err "Missing: $need - install with:  sudo pacman -S $need"
  elif command -v brew >/dev/null 2>&1; then
    err "Missing: $need - install with:  brew install $need"
  else
    err "Missing: $need - install these with your package manager, then re-run."
  fi
fi

PY="$(command -v python3)"
"$PY" - <<PYEOF || err "Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ required (found $("$PY" --version 2>&1))."
import sys
sys.exit(0 if sys.version_info[:2] >= (${NEED_PY_MAJOR}, ${NEED_PY_MINOR}) else 1)
PYEOF
say "Using $("$PY" --version)"

# The agent CLI is installed separately and needs a login; setup stops with a clear message
# if neither is present, so this is a warning only.
if ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1 \
   && [ ! -x "$HOME/.local/bin/claude" ] && [ ! -x "$HOME/.local/bin/codex" ]; then
  warn "Neither 'claude' (Claude Code) nor 'codex' (Codex CLI) found on PATH. Install one of them and log in before running setup."
fi

# 1) Get the code (use the current directory if it is a clone, else clone/download).
SRC_MARKER='agent2telegramtopic/__init__.py'
if [ -f pyproject.toml ] && [ -f "$SRC_MARKER" ] && grep -q 'name = "agent2telegramtopic"' pyproject.toml; then
  SRC="$(pwd -P)"
  say "Installing from current directory"
else
  SRC="${HOME}/.agent2telegramtopic-src"
  cd "$HOME"
  fetch_tarball() (
    say "Fetching the source archive (no git)"
    tmp="$(mktemp -d "${SRC}.stage.XXXXXX")" || return 1
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL --retry 2 --connect-timeout 15 --max-time 120 "$TARBALL" -o "$tmp/src.tgz" || return 1
    # Refuse links and paths outside the staging directory.
    "$PY" - "$tmp" <<'PYARCHIVE' || return 1
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1]).resolve()
with tarfile.open(root / 'src.tgz') as archive:
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if root not in target.parents or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe archive member')
    archive.extractall(root)
PYARCHIVE
    dir="$(find "$tmp" -maxdepth 1 -type d -name 'agent2telegramtopic-*' | head -1)"
    [ -n "$dir" ] && [ -f "$dir/agent2telegramtopic/__init__.py" ] || return 1
    if [ -e "$SRC" ]; then
      [ -f "$SRC/agent2telegramtopic/__init__.py" ] && [ ! -e "$SRC/.git" ] || return 1
      rm -rf "$SRC"
    fi
    mv "$dir" "$SRC"
  )
  if [ -L "$SRC" ]; then
    err "$SRC is a symlink; files preserved. Update its target manually."
  elif [ -d "$SRC/.git" ]; then
    say "Updating $SRC"
    [ -z "$(git -C "$SRC" status --porcelain)" ] || err "Local changes in $SRC; commit or back them up first."
    GIT_TERMINAL_PROMPT=0 git -C "$SRC" pull --ff-only || err "Update failed; existing checkout preserved."
  elif [ -e "$SRC" ]; then
    fetch_tarball || err "Archive update failed; previous files preserved."
  elif command -v git >/dev/null 2>&1; then
    say "Cloning into $SRC"
    if ! GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REPO" "$SRC" 2>/dev/null; then
      rm -rf "$SRC"
      fetch_tarball || err "Could not fetch the project."
    fi
  else
    fetch_tarball || err "Could not fetch the project (no git, and the archive download failed)."
  fi
fi

# 2) Same-sized source changes within one timestamp second must invalidate old bytecode.
"$PY" - "$SRC" <<'PYCACHE' || err "Could not refresh bytecode; installation preserved."
import compileall, pathlib, py_compile, sys
package = pathlib.Path(sys.argv[1]) / "agent2telegramtopic"
sys.exit(0 if compileall.compile_dir(str(package), quiet=1, force=True,
    invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH) else 1)
PYCACHE

# 3) Make `agent2telegramtopic` a real command (pure standard library: no pip needed).
BIND="$(pick_bindir)"
mkdir -p "$BIND"
"$PY" - "$SRC" "$PY" "$BIND/agent2telegramtopic" <<'PYLAUNCHER'
import pathlib, shlex, sys
source, interpreter, target = sys.argv[1:]
pathlib.Path(target).write_text('#!/bin/sh\nexec env ' + shlex.quote('PYTHONPATH=' + source) +
                                ' ' + shlex.quote(interpreter) + ' -m agent2telegramtopic "$@"\n')
PYLAUNCHER
chmod +x "$BIND/agent2telegramtopic"
RUN=("$BIND/agent2telegramtopic")
case ":$PATH:" in
  *":$BIND:"*)
    say "Installed launcher in $BIND (already on PATH) - 'agent2telegramtopic' works now." ;;
  *)
    say "Installed launcher in $BIND - adding it to PATH for new shells."
    for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
      [ -e "$rc" ] || continue
      grep -qs "$BIND" "$rc" || echo "export PATH=\"$BIND:\$PATH\"" >> "$rc"
    done
    # ~/.bashrc may not exist on a minimal server - create it so login shells pick it up.
    grep -qs "$BIND" "$HOME/.bashrc" 2>/dev/null || echo "export PATH=\"$BIND:\$PATH\"" >> "$HOME/.bashrc"
    export PATH="$BIND:$PATH"
    say "For THIS terminal, run:  export PATH=\"$BIND:\$PATH\"   (new terminals get it automatically)" ;;
esac

# 4) Launch the setup wizard. Under `curl ... | bash` this script's stdin is the pipe, not your
#    keyboard, so the interactive wizard reads from the controlling terminal (/dev/tty).
say "Run setup later with:  agent2telegramtopic setup"
if [ "$NO_SETUP" -eq 0 ] && [ -e /dev/tty ] && (: </dev/tty) 2>/dev/null; then
  say "Starting setup..."
  exec "${RUN[@]}" setup </dev/tty
else
  say "Installed. Finish setup with:"
  echo "    agent2telegramtopic setup"
fi
}
main "$@"; exit
