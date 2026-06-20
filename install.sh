#!/bin/sh
# ohmyboring installer (oh-my-zsh-style one-liner).
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/jazz1x/ohmyboring/main/install.sh)"
#
# Brand = ohmyboring; install dir = ~/oh-my-boring (the ohmyzsh ~/.oh-my-zsh homage; override with OMB_HOME).
# Idempotent: re-running updates the clone and skips already-wired hooks. Set OMB_WIRE=0 to skip the
# Claude Code hook/MCP wiring. POSIX sh.
set -eu

OMB_HOME="${OMB_HOME:-$HOME/oh-my-boring}"
REPO="${OMB_REPO:-https://github.com/jazz1x/ohmyboring.git}"
SETTINGS="$HOME/.claude/settings.json"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33mⓘ %s\033[0m\n' "$1"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# 1) Prerequisites — fail fast with an actionable message (start.sh re-checks in depth).
say "Checking prerequisites…"
for c in git docker jq python3 curl; do
  command -v "$c" >/dev/null 2>&1 || die "$c not found. Need: docker, jq, python3, git, curl, make (+ ollama or any OpenAI-compatible server)."
done
# `make up` (below) is this installer's entrypoint into the stack — catch a missing make here,
# not with a cryptic 'make: command not found' right after this friendly check passes.
command -v make >/dev/null 2>&1 || die "make not found — install it: macOS 'xcode-select --install' (or 'brew install make') · Debian/Ubuntu 'sudo apt-get install make' · Fedora 'sudo dnf install make'."
docker info >/dev/null 2>&1 || die "Docker daemon not running — start Docker Desktop / dockerd, then re-run."
command -v ollama >/dev/null 2>&1 || warn "ollama not on PATH — install from https://ollama.com, or set DRUDGE_LLM_BASE_URL to your own OpenAI-compatible endpoint in .env."

# 2) Clone or update (idempotent).
if [ -d "$OMB_HOME/.git" ]; then
  say "Updating existing install at $OMB_HOME"
  git -C "$OMB_HOME" pull --ff-only 2>/dev/null || warn "pull skipped (local changes present) — keeping your working tree"
else
  say "Cloning ohmyboring → $OMB_HOME"
  git clone "$REPO" "$OMB_HOME"
fi
cd "$OMB_HOME"

# 3) Bring up the stack — start.sh handles .env/boring.json creation, Ollama, model pulls, build, health.
say "Starting the stack (make up) — first run pulls models + builds (a few minutes)…"
make up

# 4) Wire Claude Code hooks + MCP — the fiddly part this installer exists to automate.
#    Idempotent + backs up settings.json. OMB_WIRE=0 to skip.
if [ "${OMB_WIRE:-1}" = 1 ]; then
  say "Wiring Claude Code hooks into $SETTINGS"
  mkdir -p "$HOME/.claude"
  [ -f "$SETTINGS" ] || printf '{}\n' > "$SETTINGS"
  if grep -q "$OMB_HOME/hooks/distill-session.py" "$SETTINGS" 2>/dev/null; then
    warn "hooks already wired — skipping"
  else
    cp "$SETTINGS" "$SETTINGS.omb-bak" && warn "backed up settings → $SETTINGS.omb-bak"
    tmp=$(mktemp)
    if jq \
        --arg distill "python3 $OMB_HOME/hooks/distill-session.py" \
        --arg recall  "python3 $OMB_HOME/hooks/recall.py" '
        .hooks //= {}
        | .hooks.SessionEnd = ((.hooks.SessionEnd // []) + [{matcher:"", hooks:[{type:"command", command:$distill, timeout:130, async:true}]}])
        | .hooks.UserPromptSubmit = ((.hooks.UserPromptSubmit // []) + [{matcher:"", hooks:[{type:"command", command:$recall, timeout:10}]}])
      ' "$SETTINGS" > "$tmp" 2>/dev/null && mv "$tmp" "$SETTINGS"; then
      say "Hooks wired: SessionEnd → distill, UserPromptSubmit → recall."
    else
      rm -f "$tmp"
      warn "Could not edit settings.json automatically — add the SessionEnd/UserPromptSubmit hooks manually (see README 'Agent adapters')."
    fi
  fi
  warn "MCP: the repo ships .mcp.json (ohmyboring-memory @ http://localhost:7700/mcp) for project scope. Global: claude mcp add --transport http --scope user ohmyboring-memory http://localhost:7700/mcp"
else
  warn "OMB_WIRE=0 — skipped Claude Code wiring. See README 'Agent adapters' to add hooks + .mcp.json yourself."
fi

printf '\n'
say "Done. ohmyboring is up. Try:"
printf '    cd %s && make ask Q="how did I fix X last time?"\n\n' "$OMB_HOME"
