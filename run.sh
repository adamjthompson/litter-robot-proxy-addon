#!/usr/bin/with-contenv bashio

export MQTT_HOST=$(bashio::config 'mqtt_host')
export MQTT_PORT=$(bashio::config 'mqtt_port')
export MQTT_USER=$(bashio::config 'mqtt_user')
export MQTT_PASS=$(bashio::config 'mqtt_pass')
export OFFLINE_THRESHOLD=$(bashio::config 'offline_threshold')

bashio::log.info "Starting Litter Robot Proxy..."
bashio::log.info "MQTT Host: ${MQTT_HOST}:${MQTT_PORT}"

exec python3 -u /app/litter-robot-proxy.py
