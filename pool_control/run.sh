#!/usr/bin/with-contenv bashio
export IAQUALINK_EMAIL="$(bashio::config 'iaqualink_email')"
export IAQUALINK_PASSWORD="$(bashio::config 'iaqualink_password')"
export IAQUALINK_SERIAL="$(bashio::config 'serial' '')"
export LOG_LEVEL="$(bashio::config 'log_level' 'info')"
export DATA_DIR=/data
cd /app
bashio::log.info "Starting Pool Control on port 8099"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --log-level "${LOG_LEVEL}"
