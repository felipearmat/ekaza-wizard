#!/usr/bin/with-contenv bashio
set -e

# Export add-on options as env vars so Python can read them
export TUYA_ACCESS_ID="$(bashio::config 'tuya_access_id')"
export TUYA_ACCESS_SECRET="$(bashio::config 'tuya_access_secret')"
export TUYA_REGION="$(bashio::config 'tuya_region' 'us')"
export RTSP_PASSWORD="$(bashio::config 'rtsp_password')"

bashio::log.info "Starting eKaza Wizard on port 7788..."
exec python3 /app/main.py
