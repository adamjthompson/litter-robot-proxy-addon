#!/usr/bin/env python3
"""
Litter Robot 3 Local MQTT Proxy
Intercepts UDP communication between LR3 Connect devices and Whisker's servers,
publishes real-time status to Home Assistant via MQTT Discovery.
"""

import datetime
import json
import os
import socket
import select
import time
import sys

import paho.mqtt.client as mqtt

# ─── Configuration ────────────────────────────────────────────────────────────

MQTT_HOST          = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT          = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER          = os.environ.get("MQTT_USER", "") or None
MQTT_PASS          = os.environ.get("MQTT_PASS", "") or None
OFFLINE_THRESHOLD  = int(os.environ.get("OFFLINE_THRESHOLD", 600))

OPTIONS_FILE  = "/data/options.json"
CYCLES_FILE   = "/data/cycles.json"

HOST_SERVER = "dispatch.prod.iothings.site"
PORT_SERVER = 2000   # we listen on this port for server responses
PORT_LITTER = 2001   # we listen on this port for robot messages

DISCOVERY_PREFIX = "homeassistant"
ADDON_ID         = "litter_robot_proxy"

# ─── Status codes ─────────────────────────────────────────────────────────────

STATUS_MAP = {
    "CCC": "Complete",
    "CCP": "Cleaning",
    "CSF": "Error",
    "SCF": "Error",
    "CSI": "Paused",
    "CST": "Waiting",
    "DF1": "Alert",
    "DF2": "Alert",
    "DFS": "Full",
    "SDF": "Full",
    "BR":  "Error",
    "P":   "Paused",
    "OFF": "Off",
    "Rdy": "Ready",
    "offline": "Offline",
}

ERROR_STATES = {"CSF", "SCF", "DFS", "SDF", "BR", "P", "OFF", "offline"}
DRAWER_FULL_STATES = {"DF1", "DF2", "DFS"}

# ─── Load options ─────────────────────────────────────────────────────────────

def load_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print("Warning: could not load options.json: %s" % e)
        return {}

options = load_options()

# Build IP → robot name map from options
robot_name_map = {}
for robot in options.get("robots", []):
    robot_name_map[robot["ip"]] = robot["name"]

def get_robot_name(device_id, ip=None):
    """Return user-configured name for a robot, falling back to device ID."""
    if ip and ip in robot_name_map:
        return robot_name_map[ip]
    return "Litter Robot %s" % device_id[-6:]

# ─── Persistent cycle storage ─────────────────────────────────────────────────

def load_cycles():
    try:
        with open(CYCLES_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_cycles(cycles):
    try:
        with open(CYCLES_FILE, "w") as f:
            json.dump(cycles, f)
    except Exception as e:
        print("Warning: could not save cycles: %s" % e)

cycles = load_cycles()

def get_cycle_count(device_id):
    return cycles.get(device_id, {}).get("count", 0)

def get_cycle_capacity(device_id):
    return cycles.get(device_id, {}).get("capacity", 30)

def increment_cycle(device_id):
    if device_id not in cycles:
        cycles[device_id] = {"count": 0, "capacity": 30}
    cycles[device_id]["count"] += 1
    save_cycles(cycles)
    return cycles[device_id]["count"]

def reset_cycle(device_id):
    if device_id not in cycles:
        cycles[device_id] = {"count": 0, "capacity": 30}
    cycles[device_id]["count"] = 0
    save_cycles(cycles)

# ─── Runtime state ────────────────────────────────────────────────────────────

robot_addresses         = {}   # device_id → (ip, port)
robot_last_seen         = {}   # device_id → timestamp
robot_offline_published = {}   # device_id → bool
robot_names             = {}   # device_id → friendly name
discovery_published     = {}   # device_id → bool
last_status             = {}   # device_id → raw status code

# ─── MQTT ─────────────────────────────────────────────────────────────────────

def on_mqtt_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker with result code: %s" % rc)
    # Subscribe to reset commands for all robots
    client.subscribe("%s/+/reset" % ADDON_ID)

def on_mqtt_message(client, userdata, msg):
    """Handle incoming MQTT messages — used for reset button presses."""
    topic = msg.topic
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == ADDON_ID and parts[2] == "reset":
        device_id = parts[1]
        print("Reset command received for %s" % device_id)
        reset_cycle(device_id)
        publish_state(device_id)

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()

# ─── MQTT Discovery ───────────────────────────────────────────────────────────

def publish_discovery(device_id, name):
    """Publish MQTT Discovery messages to auto-create HA entities for a robot."""
    if discovery_published.get(device_id):
        return

    device_info = {
        "identifiers": ["%s_%s" % (ADDON_ID, device_id)],
        "name": name,
        "model": "Litter Robot 3 Connect",
        "manufacturer": "Whisker",
    }

    base_topic = "litter_robot/%s" % device_id

    entities = [
        # Status sensor
        {
            "component": "sensor",
            "object_id": "%s_status" % device_id,
            "config": {
                "name": "Status",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.status }}",
                "icon": "mdi:emoticon-poop",
                "device": device_info,
                "unique_id": "%s_%s_status" % (ADDON_ID, device_id),
            }
        },
        # Drawer level % sensor
        {
            "component": "sensor",
            "object_id": "%s_drawer_level" % device_id,
            "config": {
                "name": "Drawer Level",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.drawer_level }}",
                "unit_of_measurement": "%",
                "icon": "mdi:delete",
                "device": device_info,
                "unique_id": "%s_%s_drawer_level" % (ADDON_ID, device_id),
            }
        },
        # Cycle count sensor
        {
            "component": "sensor",
            "object_id": "%s_cycle_count" % device_id,
            "config": {
                "name": "Cycle Count",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.cycle_count }}",
                "icon": "mdi:counter",
                "device": device_info,
                "unique_id": "%s_%s_cycle_count" % (ADDON_ID, device_id),
            }
        },
        # Wait time sensor
        {
            "component": "sensor",
            "object_id": "%s_wait_time" % device_id,
            "config": {
                "name": "Wait Time",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.wait_time }}",
                "unit_of_measurement": "min",
                "icon": "mdi:timer",
                "device": device_info,
                "unique_id": "%s_%s_wait_time" % (ADDON_ID, device_id),
            }
        },
        # Drawer full binary sensor
        {
            "component": "binary_sensor",
            "object_id": "%s_drawer_full" % device_id,
            "config": {
                "name": "Drawer Full",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.drawer_full }}",
                "payload_on": "True",
                "payload_off": "False",
                "device_class": "problem",
                "icon": "mdi:delete-alert",
                "device": device_info,
                "unique_id": "%s_%s_drawer_full" % (ADDON_ID, device_id),
            }
        },
        # Error binary sensor
        {
            "component": "binary_sensor",
            "object_id": "%s_error" % device_id,
            "config": {
                "name": "Error",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.error }}",
                "payload_on": "True",
                "payload_off": "False",
                "device_class": "problem",
                "icon": "mdi:alert",
                "device": device_info,
                "unique_id": "%s_%s_error" % (ADDON_ID, device_id),
            }
        },
        # Night light binary sensor
        {
            "component": "binary_sensor",
            "object_id": "%s_night_light" % device_id,
            "config": {
                "name": "Night Light",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.night_light }}",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:lightbulb",
                "device": device_info,
                "unique_id": "%s_%s_night_light" % (ADDON_ID, device_id),
            }
        },
        # Panel lock binary sensor
        {
            "component": "binary_sensor",
            "object_id": "%s_panel_lock" % device_id,
            "config": {
                "name": "Panel Lock",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.panel_lock }}",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:lock",
                "device": device_info,
                "unique_id": "%s_%s_panel_lock" % (ADDON_ID, device_id),
            }
        },
        # Sleep mode binary sensor
        {
            "component": "binary_sensor",
            "object_id": "%s_sleep_mode" % device_id,
            "config": {
                "name": "Sleep Mode",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.sleep_mode }}",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:sleep",
                "device": device_info,
                "unique_id": "%s_%s_sleep_mode" % (ADDON_ID, device_id),
            }
        },
        # Reset drawer button
        {
            "component": "button",
            "object_id": "%s_reset" % device_id,
            "config": {
                "name": "Reset Drawer Counter",
                "command_topic": "%s/%s/reset" % (ADDON_ID, device_id),
                "payload_press": "reset",
                "icon": "mdi:restore",
                "device": device_info,
                "unique_id": "%s_%s_reset" % (ADDON_ID, device_id),
            }
        },
    ]

    for entity in entities:
        topic = "%s/%s/%s_%s/config" % (
            DISCOVERY_PREFIX,
            entity["component"],
            ADDON_ID,
            entity["object_id"]
        )
        mqtt_client.publish(topic, json.dumps(entity["config"]), retain=True)

    discovery_published[device_id] = True
    print("Published MQTT Discovery for %s (%s)" % (name, device_id))

# ─── State publishing ─────────────────────────────────────────────────────────

def publish_state(device_id, raw_status=None, parsed=None):
    """Publish unified state message for a robot."""
    if raw_status is None:
        raw_status = last_status.get(device_id, "offline")

    cycle_count    = get_cycle_count(device_id)
    cycle_capacity = get_cycle_capacity(device_id)
    drawer_level   = min(round((cycle_count / cycle_capacity) * 100), 100)

    # Parse wait time from W7 → 7
    wait_time = 0
    if parsed and parsed.get("wait"):
        try:
            wait_str = parsed["wait"]
            if wait_str.startswith("W"):
                val = wait_str[1:]
                wait_time = 15 if val == "F" else int(val)
        except:
            pass

    state = {
        "status":       STATUS_MAP.get(raw_status, "Error"),
        "raw_status":   raw_status,
        "cycle_count":  cycle_count,
        "drawer_level": drawer_level,
        "drawer_full":  str(raw_status in DRAWER_FULL_STATES),
        "error":        str(raw_status in ERROR_STATES),
        "night_light":  str(parsed.get("light") == "NL1") if parsed else "False",
        "panel_lock":   str(parsed.get("lock") == "PL1") if parsed else "False",
        "sleep_mode":   str(parsed.get("sleep_mode") == "SM1") if parsed else "False",
        "wait_time":    wait_time,
        "device_id":    device_id,
        "ts":           int(time.time()),
    }

    mqtt_client.publish(
        "litter_robot/%s/state" % device_id,
        json.dumps(state),
        retain=True
    )

# ─── Watchdog ─────────────────────────────────────────────────────────────────

def check_offline():
    now = time.time()
    for device_id, last_seen in robot_last_seen.items():
        if now - last_seen > OFFLINE_THRESHOLD:
            if not robot_offline_published.get(device_id, False):
                name = robot_names.get(device_id, device_id)
                print("%s WATCHDOG: %s (%s) has not reported in %ds - marking offline" % (
                    datetime.datetime.now().isoformat(),
                    name,
                    device_id,
                    int(now - last_seen)
                ))
                last_status[device_id] = "offline"
                publish_state(device_id, raw_status="offline")
                robot_offline_published[device_id] = True

# ─── Packet handlers ──────────────────────────────────────────────────────────

def handle_from_robot(raw_data, addr):
    try:
        msg = raw_data.strip().decode()
    except:
        print("handle_from_robot: error parsing data from %s" % str(addr))
        return

    parts = msg.split(",")

    if len(parts) == 12:
        device_id  = parts[1]
        raw_status = parts[4]
        ip         = addr[0]

        # Determine robot name
        name = robot_name_map.get(ip, "Litter Robot %s" % device_id[-6:])
        robot_names[device_id] = name

        # Log only on first seen or IP change
        if device_id not in robot_addresses or robot_addresses[device_id] != addr:
            print("%s Tracking %s (%s) at %s" % (
                datetime.datetime.now().isoformat(), name, device_id, ip
            ))

        robot_addresses[device_id]         = addr
        robot_last_seen[device_id]         = time.time()
        robot_offline_published[device_id] = False

        parsed = {
            "wait":       parts[5],
            "light":      parts[6],
            "sleep_mode": parts[7][0:3],
            "lock":       parts[8],
        }

        # Publish discovery on first contact
        publish_discovery(device_id, name)

        # Track cycle completions
        prev_status = last_status.get(device_id)
        if raw_status == "CCC" and prev_status != "CCC":
            count = increment_cycle(device_id)
            print("%s Cycle complete for %s — count: %d" % (
                datetime.datetime.now().isoformat(), name, count
            ))

        last_status[device_id] = raw_status

        print("%-27s %-16s %5d FROM_LITTER     %s" % (
            datetime.datetime.now().isoformat(), ip, addr[1], msg
        ))

        publish_state(device_id, raw_status=raw_status, parsed=parsed)

    elif len(parts) == 6:
        device_id = parts[1]
        ip        = addr[0]

        if device_id not in robot_addresses or robot_addresses[device_id] != addr:
            name = robot_name_map.get(ip, "Litter Robot %s" % device_id[-6:])
            print("%s Tracking %s (%s) at %s" % (
                datetime.datetime.now().isoformat(), name, device_id, ip
            ))

        robot_addresses[device_id]         = addr
        robot_last_seen[device_id]         = time.time()
        robot_offline_published[device_id] = False

    # Relay upstream unchanged
    sock_litter.sendto(raw_data, (HOST_SERVER, 2001))

def handle_from_server(raw_data, addr):
    try:
        msg = raw_data.strip().decode()
    except:
        print("handle_from_server: error parsing data from %s" % str(addr))
        return

    parts = msg.split(",")

    target_addr = None
    if len(parts) == 5:
        device_id   = parts[2]
        target_addr = robot_addresses.get(device_id)
        print("%-27s %-16s %5d FROM_SERVER     %s" % (
            datetime.datetime.now().isoformat(), addr[0], addr[1], msg
        ))
    elif len(parts) == 2 and parts[0] in ("AOK", "NOK"):
        device_id   = parts[1]
        target_addr = robot_addresses.get(device_id)

    if target_addr:
        sock_server.sendto(raw_data, (target_addr[0], 2000))
    else:
        print("ERROR: No address known for device, cannot forward: %s" % msg)

# ─── Sockets ──────────────────────────────────────────────────────────────────

sock_litter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_litter.bind(("0.0.0.0", PORT_LITTER))

sock_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_server.bind(("0.0.0.0", PORT_SERVER))

print("Litter Robot Proxy started")
print("Listening on UDP ports %d (robots) and %d (server responses)" % (PORT_LITTER, PORT_SERVER))
print("Relaying upstream to %s" % HOST_SERVER)
print("Offline threshold: %ds" % OFFLINE_THRESHOLD)
if robot_name_map:
    print("Configured robots:")
    for ip, name in robot_name_map.items():
        print("  %s → %s" % (ip, name))
else:
    print("No robots configured — will auto-discover from traffic")

# ─── Main loop ────────────────────────────────────────────────────────────────

while True:
    read, _, _ = select.select([sock_litter, sock_server], [], [], 60)

    if not read:
        check_offline()
        continue

    for r in read:
        data, addr = r.recvfrom(1024)
        if   r == sock_litter: handle_from_robot(data, addr)
        elif r == sock_server: handle_from_server(data, addr)
