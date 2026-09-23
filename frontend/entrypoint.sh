#!/bin/bash
set -euo pipefail

# app.py lives in grc/ (see pyproject.toml's [tool.setuptools] comment on
# why) -- cd there so `uvicorn app:app` resolves it directly instead of
# needing grc/ to be an importable package (__init__.py) at runtime.
cd "$(dirname "$0")/grc"
exec uvicorn app:app --host "${A2K_FRONTEND_HOST:-0.0.0.0}" --port "${A2K_FRONTEND_PORT:-8080}"
