#!/bin/bash
set -euo pipefail

exec uvicorn a2k.api.rest:app --host "${A2K_BOX_HOST:-0.0.0.0}" --port "${A2K_BOX_PORT:-8000}"
