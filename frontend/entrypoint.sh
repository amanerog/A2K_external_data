#!/bin/bash
set -euo pipefail

exec uvicorn app:app --host "${A2K_FRONTEND_HOST:-0.0.0.0}" --port "${A2K_FRONTEND_PORT:-8080}"
