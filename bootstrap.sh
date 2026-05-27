#!/usr/bin/env bash
# Bootstrap the sprint-loop venv via uv (https://docs.astral.sh/uv/).
#
# Usage: dev/sprint-loop/bootstrap.sh
#
# Idempotent: subsequent runs just `uv sync` against the lock file.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv sync --all-extras
echo
echo "[bootstrap] venv ready at $(pwd)/.venv"
echo "[bootstrap] try:  uv run forge-loop config"
