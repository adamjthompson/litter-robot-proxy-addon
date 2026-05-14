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

import re

import paho.mqtt.client as mqtt

def slugify(name):
    """Convert a friendly name like 'Litter Robot 1' → 'litter_robot_1'"""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

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
    "CSF": "Cat Sensor Fault",
    "SCF": "Cat Sensor Fault",
    "CSI": "Cat Interrupted",
    "CST": "Waiting",
    "DF1": "Almost Full",
    "DF2": "Nearly Full",
    "DFS": "Full",
    "SDF": "Full",
    "BR":  "Bonnet Removed",
    "P":   "Paused",
    "OFF": "Off",
    "Rdy": "Ready",
    "offline": "Offline",
}

ERROR_STATES       = {"CSF", "SCF", "BR", "P", "OFF", "offline"}
DRAWER_FULL_STATES = {"DFS", "SDF"}

# ─── Load options ─────────────────────────────────────────────────────────────

def load_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print("Warning: could not load options.json: %s" % e)
        return {}

options = load_options()

# ─── Validate required configuration ─────────────────────────────────────────

def validate_options(opts):
    errors = []
    if not opts.get("mqtt_user", "").strip():
        errors.append("mqtt_user is required but not set.")
    if not opts.get("mqtt_pass", "").strip():
        errors.append("mqtt_pass is required but not set.")
    if not opts.get("robots"):
        errors.append("No robots configured. Add at least one robot IP and name.")
    if errors:
        print("ERROR: Add-on configuration is incomplete:")
        for e in errors:
            print("  - %s" % e)
        print("Please update the add-on configuration and restart.")
        sys.exit(1)

validate_options(options)

# Build IP → robot name/capacity map from options
robot_name_map          = {}   # ip → name
robot_capacity_map      = {}   # ip → capacity
robot_explicit_capacity = {}   # ip → bool (True if user set a specific >0 value in UI)
robot_device_map        = {}   # ip → device_id (optional, from config)
ip_from_device          = {}   # device_id → ip (reverse lookup)
robot_names             = {}   # device_id → friendly name

for robot in options.get("robots", []):
    ip   = robot["ip"]
    name = robot["name"]
    robot_name_map[ip] = name
    
    # Check if capacity was left blank, missing, or explicitly set to 0 (Auto Mode)
    raw_cap = robot.get("capacity")
    if raw_cap is None or raw_cap <= 0:
        robot_capacity_map[ip]      = 30 # Baseline starting point for auto-learn
        robot_explicit_capacity[ip] = False
    else:
        robot_capacity_map[ip]      = raw_cap
        robot_explicit_capacity[ip] = True

    device_id = robot.get("device_id", "").strip()
    if device_id:
        robot_device_map[ip]          = device_id
        ip_from_device[device_id]     = ip
        robot_names[device_id]        = name

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
        cycles[device_id] = {"count": 0, "capacity": 30, "history": []}
    cycles[device_id]["count"] += 1
    save_cycles(cycles)
    return cycles[device_id]["count"]

def reset_cycle(device_id):
    if device_id not in cycles:
        cycles[device_id] = {"count": 0, "capacity": 30, "history": []}
    cycles[device_id]["count"] = 0
    save_cycles(cycles)

# ─── Runtime state ────────────────────────────────────────────────────────────

robot_addresses         = {}   # device_id → (ip, port)
robot_last_seen         = {}   # device_id → timestamp
robot_offline_published = {}   # device_id → bool
discovery_published     = {}   # device_id → bool
last_status             = {}   # device_id → raw status code
suppress_until          = {}   # device_id → timestamp (for power cycle boots)

# ─── MQTT ─────────────────────────────────────────────────────────────────────

def on_mqtt_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker with result code: %s" % rc)
    client.subscribe("%s/+/reset" % ADDON_ID, qos=1)
    client.subscribe("%s/+/power_cycled" % ADDON_ID, qos=1)
    time.sleep(1)
    for device_id in cycles.keys():
        name = robot_names.get(device_id)
        cleanup_old_discovery(device_id, name=name)

def on_mqtt_message(client, userdata, msg):
    """Handle incoming MQTT messages — used for resets and power cycle suppressions."""
    topic = msg.topic
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == ADDON_ID:
        device_id = parts[1]
        command = parts[2]
        
        if command == "reset":
            print("Reset command received for %s" % device_id)
            reset_cycle(device_id)
            publish_state(device_id)
            
        elif command == "power_cycled":
            print("Power cycle notification received for %s. Suppressing next cycle count for 3 minutes." % device_id)
            suppress_until[device_id] = time.time() + 180

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()

# ─── MQTT Discovery cleanup ───────────────────────────────────────────────────

def cleanup_old_discovery(device_id, name=None):
    components_suffixes = [
        ("sensor",        "status"),
        ("sensor",        "drawer_level"),
        ("sensor",        "cycle_count"),
        ("sensor",        "wait_time"),
        ("binary_sensor", "drawer_full"),
        ("binary_sensor", "error"),
        ("binary_sensor", "night_light"),
        ("binary_sensor", "panel_lock"),
        ("binary_sensor", "sleep_mode"),
        ("button",        "reset"),
    ]
    for component, suffix in components_suffixes:
        topic = "%s/%s/litter_robot_%s_%s/config" % (DISCOVERY_PREFIX, component, device_id, suffix)
        mqtt_client.publish(topic, "", retain=True)
    short_id = device_id[-6:]
    for component, suffix in components_suffixes:
        topic = "%s/%s/litter_robot_%s_%s/config" % (DISCOVERY_PREFIX, component, short_id, suffix)
        mqtt_client.publish(topic, "", retain=True)
    for component, suffix in components_suffixes:
        topic = "%s/%s/%s_%s_%s/config" % (DISCOVERY_PREFIX, component, ADDON_ID, device_id, suffix)
        mqtt_client.publish(topic, "", retain=True)
    if name:
        slug = slugify(name)
        for component, suffix in components_suffixes:
            topic = "%s/%s/%s_%s_%s/config" % (DISCOVERY_PREFIX, component, ADDON_ID, slug, suffix)
            mqtt_client.publish(topic, "", retain=True)
    print("Cleaned up stale discovery messages for device %s" % device_id)

# ─── MQTT Discovery ───────────────────────────────────────────────────────────

def publish_discovery(device_id, name):
    if discovery_published.get(device_id):
        return

    slug = slugify(name)

    device_info = {
        "identifiers": ["%s_%s" % (ADDON_ID, device_id)],
        "name": name,
        "model": "Litter Robot 3 Connect",
        "manufacturer": "Whisker",
    }

    base_topic = "litter_robot/%s" % device_id

    entities = [
        {
            "component": "sensor",
            "object_id": "%s_%s_status" % (ADDON_ID, slug),
            "config": {
                "name": "Status",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.status }}",
                "icon": "mdi:emoticon-poop",
                "device": device_info,
                "unique_id": "%s_%s_status" % (ADDON_ID, device_id),
            }
        },
        {
            "component": "sensor",
            "object_id": "%s_%s_drawer_level" % (ADDON_ID, slug),
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
        {
            "component": "sensor",
            "object_id": "%s_%s_cycle_count" % (ADDON_ID, slug),
            "config": {
                "name": "Cycle Count",
                "state_topic": "%s/state" % base_topic,
                "value_template": "{{ value_json.cycle_count }}",
                "icon": "mdi:counter",
                "device": device_info,
                "unique_id": "%s_%s_cycle_count" % (ADDON_ID, device_id),
            }
        },
        {
            "component": "sensor",
            "object_id": "%s_%s_wait_time" % (ADDON_ID, slug),
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
        {
            "component": "binary_sensor",
            "object_id": "%s_%s_drawer_full" % (ADDON_ID, slug),
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
        {
            "component": "binary_sensor",
            "object_id": "%s_%s_error" % (ADDON_ID, slug),
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
        {
            "component": "binary_sensor",
            "object_id": "%s_%s_night_light" % (ADDON_ID, slug),
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
        {
            "component": "binary_sensor",
            "object_id": "%s_%s_panel_lock" % (ADDON_ID, slug),
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
        {
            "component": "binary_sensor",
            "object_id": "%s_%s_sleep_mode" % (ADDON_ID, slug),
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
        {
            "component": "button",
            "object_id": "%s_%s_reset" % (ADDON_ID, slug),
            "config": {
                "name": "Reset Drawer Counter",
                "command_topic": "%s/%s/reset" % (ADDON_ID, device_id),
                "payload_press": "reset",
                "qos": 1,
                "icon": "mdi:restore",
                "device": device_info,
                "unique_id": "%s_%s_reset" % (ADDON_ID, device_id),
            }
        },
    ]

    for entity in entities:
        topic = "%s/%s/%s/config" % (DISCOVERY_PREFIX, entity["component"], entity["object_id"])
        mqtt_client.publish(topic, json.dumps(entity["config"]), retain=True)

    discovery_published[device_id] = True
    print("Published MQTT Discovery for %s (%s) → slug: %s" % (name, device_id, slug))

# ─── State publishing ─────────────────────────────────────────────────────────

def publish_state(device_id, raw_status=None, parsed=None, name=None):
    if raw_status is None:
        raw_status = last_status.get(device_id, "offline")
    if name is None:
        name = robot_names.get(device_id, "")

    cycle_count    = get_cycle_count(device_id)
    cycle_capacity = get_cycle_capacity(device_id)
    drawer_level   = min(round((cycle_count / cycle_capacity) * 100), 100) if cycle_capacity > 0 else 0

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
        "name":         name,
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

    mqtt_client.publish("litter_robot/%s/state" % device_id, json.dumps(state), retain=True)

# ─── Watchdog ─────────────────────────────────────────────────────────────────

def check_upstream():
    try:
        import socket as _socket
        _socket.getaddrinfo(HOST_SERVER, 2001)
        print("%s UPSTREAM: %s reachable" % (datetime.datetime.now().isoformat(), HOST_SERVER))
        return True
    except Exception as e:
        print("%s UPSTREAM WARNING: Cannot reach %s: %s" % (datetime.datetime.now().isoformat(), HOST_SERVER, str(e)))
        return False

def check_offline():
    now = time.time()
    checked = []
    for device_id, last_seen in list(robot_last_seen.items()):
        name = robot_names.get(device_id, device_id)
        elapsed = int(now - last_seen)
        if now - last_seen > OFFLINE_THRESHOLD:
            checked.append("%s (offline %ds)" % (name, elapsed))
            if not robot_offline_published.get(device_id, False):
                is_placeholder = device_id.startswith("pending_")
                if is_placeholder:
                    print("%s WATCHDOG: %s has not connected since startup — add device_id to config for proper offline detection" % (
                        datetime.datetime.now().isoformat(), name
                    ))
                else:
                    print("%s WATCHDOG: %s (%s) has not reported in %ds - marking offline" % (
                        datetime.datetime.now().isoformat(), name, device_id, elapsed
                    ))
                    last_status[device_id] = "offline"
                    publish_state(device_id, raw_status="offline", name=name)
                robot_offline_published[device_id] = True
        else:
            checked.append("%s (ok, %ds ago)" % (name, elapsed))
    if checked:
        print("%s WATCHDOG: %s" % (datetime.datetime.now().isoformat(), ", ".join(checked)))
    check_upstream()

# ─── Packet handlers ──────────────────────────────────────────────────────────

def handle_from_robot(raw_data, addr):
    try:
        msg = raw_data.strip().decode()
    except:
        print("handle_from_robot: error parsing data from %s" % str(addr))
        sock_litter.sendto(raw_data, (HOST_SERVER, 2001))
        return

    parts = msg.split(",")

    if len(parts) == 12:
        device_id  = parts[1]
        raw_status = parts[4]
        ip         = addr[0]

        name = robot_name_map.get(ip)
        if not name:
            print("%s WARNING: Robot at %s (device %s) is not configured. Add this IP to the add-on configuration." % (
                datetime.datetime.now().isoformat(), ip, device_id
            ))
            sock_litter.sendto(raw_data, (HOST_SERVER, 2001))
            return

        robot_names[device_id] = name

        # Initialize cycle tracking if it doesn't exist
        if device_id not in cycles:
            cycles[device_id] = {"count": 0, "capacity": robot_capacity_map.get(ip, 30), "history": []}
        cycles[device_id]["ip"] = ip
        if "history" not in cycles[device_id]:
            cycles[device_id]["history"] = []
        
        # Enforce UI capacity ONLY if the user explicitly typed a number > 0
        if robot_explicit_capacity.get(ip, False):
            config_capacity = robot_capacity_map[ip]
            if cycles[device_id].get("capacity") != config_capacity:
                cycles[device_id]["capacity"] = config_capacity
                save_cycles(cycles)

        if device_id not in robot_addresses or robot_addresses[device_id] != addr:
            print("%s Tracking %s (%s) at %s" % (datetime.datetime.now().isoformat(), name, device_id, ip))
            placeholder_id = "pending_%s" % ip.replace(".", "_")
            robot_last_seen.pop(placeholder_id, None)
            robot_names.pop(placeholder_id, None)
            robot_offline_published.pop(placeholder_id, None)
            ip_from_device[device_id] = ip

        robot_addresses[device_id]         = addr
        robot_last_seen[device_id]         = time.time()
        robot_offline_published[device_id] = False

        parsed = {
            "wait":       parts[5],
            "light":      parts[6],
            "sleep_mode": parts[7][0:3],
            "lock":       parts[8],
        }

        if not discovery_published.get(device_id):
            cleanup_old_discovery(device_id, name=name)
        publish_discovery(device_id, name)

        # Track cycle completions (with boot cycle suppression)
        prev_status = last_status.get(device_id)
        if raw_status == "CCC" and prev_status != "CCC":
            if time.time() < suppress_until.get(device_id, 0):
                print("%s Boot cycle suppressed for %s — smart plug was recently cycled." % (datetime.datetime.now().isoformat(), name))
            else:
                count = increment_cycle(device_id)
                print("%s Cycle complete for %s — count: %d" % (datetime.datetime.now().isoformat(), name, count))

        # --- Dynamic Capacity Learning (5-Cycle Moving Average) ---
        if raw_status in DRAWER_FULL_STATES and prev_status not in DRAWER_FULL_STATES:
            # Only auto-learn if the user left UI capacity blank or 0
            if not robot_explicit_capacity.get(ip, False):
                current_count = get_cycle_count(device_id)
                if current_count > 0: # Prevent dividing by zero
                    
                    # Append new count and keep only the last 5
                    cycles[device_id]["history"].append(current_count)
                    cycles[device_id]["history"] = cycles[device_id]["history"][-5:]
                    
                    # Calculate new average capacity
                    history = cycles[device_id]["history"]
                    new_avg = max(1, round(sum(history) / len(history)))
                    
                    cycles[device_id]["capacity"] = new_avg
                    save_cycles(cycles)
                    print("%s Auto-learned capacity for %s: updated to %d (avg of last %d cycles: %s)" % (
                        datetime.datetime.now().isoformat(), name, new_avg, len(history), str(history)
                    ))

        last_status[device_id] = raw_status

        print("%-27s %-16s %5d FROM_LITTER     %s" % (
            datetime.datetime.now().isoformat(), ip, addr[1], msg
        ))

        publish_state(device_id, raw_status=raw_status, parsed=parsed)

    elif len(parts) == 6:
        device_id = parts[1]
        ip        = addr[0]

        name = robot_name_map.get(ip)
        if name:
            if device_id not in robot_addresses or robot_addresses[device_id] != addr:
                print("%s Tracking %s (%s) at %s" % (datetime.datetime.now().isoformat(), name, device_id, ip))
            robot_addresses[device_id]         = addr
            robot_last_seen[device_id]         = time.time()
            robot_offline_published[device_id] = False

    try:
        sock_litter.sendto(raw_data, (HOST_SERVER, 2001))
    except Exception as e:
        print("%s ERROR: Failed to relay to upstream server: %s" % (datetime.datetime.now().isoformat(), str(e)))

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
        print("%-27s %-16s %5d FROM_SERVER     %s" % (datetime.datetime.now().isoformat(), addr[0], addr[1], msg))
    elif len(parts) == 2 and parts[0] in ("AOK", "NOK"):
        device_id   = parts[1]
        target_addr = robot_addresses.get(device_id)

    if target_addr:
        try:
            sock_server.sendto(raw_data, (target_addr[0], 2000))
        except Exception as e:
            print("%s ERROR: Failed to relay to robot at %s: %s" % (datetime.datetime.now().isoformat(), target_addr[0], str(e)))
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
        device_id = robot_device_map.get(ip)
        if device_id:
            print("  %s → %s (device: %s)" % (ip, name, device_id))
        else:
            print("  %s → %s (no device_id configured)" % (ip, name))
    startup_ts = time.time() - OFFLINE_THRESHOLD
    for ip, name in robot_name_map.items():
        device_id = robot_device_map.get(ip)
        if device_id:
            robot_last_seen[device_id]         = startup_ts
            robot_names[device_id]             = name
            robot_offline_published[device_id] = True
            publish_state(device_id, raw_status="offline", name=name)
            print("  Publishing offline for %s (%s) → topic: litter_robot/%s/state" % (name, ip, device_id))
        else:
            placeholder_id = "pending_%s" % ip.replace(".", "_")
            robot_last_seen[placeholder_id]         = startup_ts
            robot_names[placeholder_id]             = name
            robot_offline_published[placeholder_id] = True
            print("  No device_id for %s (%s) — add device_id to config for immediate offline detection" % (name, ip))
else:
    print("WARNING: No robots configured. Add robot IPs to the add-on configuration.")

# ─── Main loop ────────────────────────────────────────────────────────────────

WATCHDOG_INTERVAL = 60  
last_watchdog = time.time()

while True:
    read, _, _ = select.select([sock_litter, sock_server], [], [], 10)

    now = time.time()
    if now - last_watchdog >= WATCHDOG_INTERVAL:
        check_offline()
        last_watchdog = now

    if not read:
        continue

    for r in read:
        data, addr = r.recvfrom(1024)
        if   r == sock_litter: handle_from_robot(data, addr)
        elif r == sock_server: handle_from_server(data, addr)