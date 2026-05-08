# Changelog

## 1.0.5
- Changed drawer full sensor to only trigger when FULL instead of almost full.
- Changed error sensor to NOT include drawer full state.

## 1.0.4
- Added descriptions in Setup UI

## 1.0.3
- Added configurable capacity per robot
- Added validation to prevent starting without MQTT credentials
- Improved MQTT discovery entity naming using friendly names
- Fixed reset button reliability with QoS 1
- Improved startup ordering for boot reliability