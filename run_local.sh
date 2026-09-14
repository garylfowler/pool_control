#!/usr/bin/env bash
# Run the add-on on this Mac against the real Home Assistant and iAqualink.
# Needs a .env file with IAQUALINK_EMAIL, IAQUALINK_PASSWORD, HA_TOKEN (long-lived token) and optionally HA_URL.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export DATA_DIR="${DATA_DIR:-./data}"
export PYTHONPATH=pool_control
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --reload --reload-dir pool_control
