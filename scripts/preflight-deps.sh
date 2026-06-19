#!/bin/sh
# Dependency preflight — fail fast (before pulling GBs of models / docker compose)
# if a required host tool is missing. Exits 1 on the FIRST missing dep with an
# actionable install hint, so a clone-and-run user sees a clear message instead
# of a cryptic exit 127 deep into setup.
set -eu

# Print an actionable hint and abort on the first missing dependency.
missing() {
    name="$1"
    hint="$2"
    echo "✗ Required dependency not found: ${name}" >&2
    echo "  ${hint}" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || missing "docker" \
    "Install Docker Desktop (macOS/Windows) or Docker Engine (Linux): https://docs.docker.com/get-docker/"

# `docker` on PATH is not enough — the daemon must be live, or compose fails later.
docker info >/dev/null 2>&1 || missing "docker daemon" \
    "Docker is installed but the daemon is not responding. Start Docker Desktop, or run: sudo systemctl start docker"

command -v jq >/dev/null 2>&1 || missing "jq" \
    "Install jq: macOS 'brew install jq' · Debian/Ubuntu 'sudo apt-get install jq' · Fedora 'sudo dnf install jq' · https://jqlang.github.io/jq/download/"

command -v curl >/dev/null 2>&1 || missing "curl" \
    "Install curl: macOS 'brew install curl' · Debian/Ubuntu 'sudo apt-get install curl' · Fedora 'sudo dnf install curl'"

command -v python3 >/dev/null 2>&1 || missing "python3" \
    "Install Python 3: macOS 'brew install python3' · Debian/Ubuntu 'sudo apt-get install python3' · Fedora 'sudo dnf install python3' · https://www.python.org/downloads/"

# `make` drives the whole stack (make up → start.sh → this script); a fresh box without it
# otherwise dies with a cryptic 'make: command not found' before any of these checks run.
command -v make >/dev/null 2>&1 || missing "make" \
    "Install make: macOS 'xcode-select --install' (or 'brew install make') · Debian/Ubuntu 'sudo apt-get install make' · Fedora 'sudo dnf install make'"

echo "✓ Dependencies OK (docker + daemon, jq, curl, python3, make)"
