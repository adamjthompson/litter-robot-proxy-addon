# Changelog

## 1.1.6
- Fixed placeholder entity names that were being passed through to Node-RED notifications

## 1.1.5
- Fixed startup issues after device_ids were added

## 1.1.4
- On startup, if device_id is configured, publishes offline directly to the real MQTT topic immediately
- Placeholder fallback now works for robots without a configured device_id
- Placeholder entries are now cleared when robot first connects

## 1.1.3
- Added logic to catch offline robots at startup

## 1.1.2
- Updated watchdog to log every time it runs, making it easier to track errors