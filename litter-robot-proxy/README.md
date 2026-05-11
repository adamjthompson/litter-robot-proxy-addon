# Litter Robot Proxy — Home Assistant Add-On

This is a local MQTT proxy for **Litter Robot 3 Connect** devices. It intercepts UDP communication between your robots and Whisker's servers, publishing real-time status to Home Assistant via MQTT Discovery — no cloud dependency, no polling, no API credentials required.

## How it works

Your Litter Robot 3 connects to Whisker's servers at `dispatch.prod.iothings.site` on UDP port 2001. This add-on sits in the middle:

```
LR3 → (DNS rewrite) → This add-on → Whisker servers (upstream relay)
                             ↓
                      MQTT Discovery
                             ↓
                     Home Assistant entities
```

The robots continue communicating with Whisker normally — the Whisker app keeps working. You simply gain real-time local visibility on top. *Note that this does NOT allow for robot **control**, only robot monitoring.*

## Prerequisites

- **Mosquitto broker** add-on installed in Home Assistant.
- **AdGuard Home** (or another local DNS server) to redirect robot DNS queries. *This might also be possible directly on your router, if you know how.*

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the **⋮** menu → **Repositories**
3. Add this repository URL
4. Find **Litter Robot Proxy** and click **Install**

## Required: DNS Rewrite

Your Litter Robot 3 devices must resolve `dispatch.prod.iothings.site` to your Home Assistant IP address instead of Whisker's servers.

**In AdGuard Home:**
1. Go to **Filters → DNS Rewrites**
2. Click **Add DNS Rewrite**
3. Domain: `dispatch.prod.iothings.site`
4. Answer: `<your Home Assistant IP>`

**Your IoT VLAN must also use AdGuard for DNS.** In your router/controller, set the DHCP DNS server for your IoT network to your AdGuard IP.

After adding the rewrite, power cycle your Litter Robot(s). Check the AdGuard query log to confirm the robots' DNS queries show as **Rewritten**.

## Configuration

```yaml
mqtt_host: "core-mosquitto"   # Use 'core-mosquitto' for the Mosquitto add-on
mqtt_port: 1883
mqtt_user: ""                  # Your MQTT username
mqtt_pass: ""                  # Your MQTT password
offline_threshold: 600         # Seconds before a robot is marked offline (default: 10 min)
robots:
  - name: "Litter Robot 1"    # Friendly name shown in Home Assistant
    ip: "192.168.1.101"       # Static IP of this robot on your network
    capacity: 25              # Number of cycles before showing 100% (default is 30 if left blank)
  - name: "Litter Robot 2"
    ip: "192.168.1.102"
  - name: "Litter Robot 3"
    ip: "192.168.1.103"
```

> **Tip:** Assign static DHCP leases to your Litter Robots in your router so their IPs never change.

> **Note:** If you don't configure any robots, the add-on will still work — it auto-discovers robots from traffic and names them by their device ID. Adding them by IP just gives them friendly names.

## Entities created per robot

The add-on uses MQTT Discovery to automatically create the following entities in Home Assistant, grouped under a single device per robot:

| Entity | Type | Description |
|--------|------|-------------|
| Status | Sensor | Ready / Cleaning / Waiting / Paused / Paused - Cat Interrupted / Complete / Alert - Almost Full / Alert - Nearly Full / Full / Error - Bonnet Removed / Error - Cat Sensor Fault / Offline / Off |
| Drawer Level | Sensor | Estimated fill percentage based on cycle count |
| Cycle Count | Sensor | Number of cleaning cycles since last reset |
| Wait Time | Sensor | Configured wait time in minutes |
| Drawer Full | Binary Sensor | On when status is DF1, DF2, or DFS |
| Error | Binary Sensor | On when any error condition is active |
| Night Light | Binary Sensor | Night light on/off state |
| Panel Lock | Binary Sensor | Panel lock on/off state |
| Sleep Mode | Binary Sensor | Sleep mode on/off state |
| Reset Drawer Counter | Button | Resets cycle count to zero for this robot |

## Drawer level calibration

The drawer level percentage is estimated from cycle count divided by a configurable capacity (default: 30 cycles). This default is a reasonable starting point but varies based on:

- Number of cats using each robot
- Litter depth
- Cat size

To calibrate per robot:
1. Empty a drawer and press **Reset Drawer Counter**
2. Note the cycle count when the robot reports `DF1` (drawer full warning)
3. That number is your robot's actual capacity

30 cycles is a conservative default that will trend toward showing fuller than reality rather than missing a full drawer. The number of cycles can be configured for each robot in the UI.

## Offline detection

If a robot stops reporting for longer than `offline_threshold` seconds (default 10 minutes), its Status sensor changes to `Offline` and the Error binary sensor turns on. When the robot comes back online, status updates automatically.

## Troubleshooting

**Robots not appearing in Home Assistant:**
- Check the add-on log for connection messages
- Verify the DNS rewrite is active in AdGuard
- Confirm robots are using AdGuard for DNS (check AdGuard query log for robot IPs)
- Power cycle the robots after setting up the DNS rewrite

**Drawer level not incrementing:**
- The cycle count only increments on `CCC` (cycle complete) events
- Verify the robot is completing full cycles (not being interrupted)

**Whisker app stopped working:**
- The add-on relays all traffic to Whisker's servers transparently
- If the app stops working, check the add-on log for upstream relay errors
- The Whisker app's reliability depends on Whisker's cloud infrastructure

## Notes

- Cycle counts persist across add-on restarts in `/data/cycles.json`
- The add-on does **not** send commands to the robots — monitoring only
- This add-on is not affiliated with Whisker or Litter Robot
