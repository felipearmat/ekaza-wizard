#!/usr/bin/with-contenv bashio
set -e
bashio::log.info "Starting eKaza Wizard on port 7788..."
exec python3 /app/main.py
