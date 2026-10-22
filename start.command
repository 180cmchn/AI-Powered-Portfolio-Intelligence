#!/bin/zsh
set -e
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv sync --frozen
  else
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
fi
.venv/bin/python scripts/manage.py start "$@"
