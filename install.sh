#!/usr/bin/env bash
# Better Auto-Compact for Claude Code — Installer
# https://github.com/jsvoboda/claude-better-compact
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Require Python 3
if ! command -v python3 &>/dev/null; then
  echo "Error: python3 is required but not found." >&2
  exit 1
fi

python3 "${SCRIPT_DIR}/src/better_compact.py" install "$@"
