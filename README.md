# shark2mqtt

Bridge SharkNinja robot vacuums to [Home Assistant](https://www.home-assistant.io/) via MQTT autodiscovery.

> **Note:** This project has been developed and tested with US-region Shark accounts and two specific models:
> - **Shark AI Ultra Robot Vacuum with Self-Empty Base** (UR250BEXUS)
> - **Shark Matrix Plus Robot Vacuum and Mop with Self-Empty HEPA Base** (UR2360EEUS)
>
> It may work with other SharkNinja robot vacuums, but no other models have been tested. EU region configuration is included (`SHARK_REGION=eu`) but is completely untested and may not work.

## Quick Start

1. Copy the example config and fill in your credentials:

   ```bash
   cp config.example.env .env
   # Edit .env with your Shark account and MQTT broker details
   ```

2. Run with Docker Compose:

   ```bash
   docker compose up -d
   ```

## Pre-built Image

A pre-built image is available from GitHub Container Registry:

```bash
docker run -d \
  --name shark2mqtt \
  --env-file .env \
  -v shark2mqtt_data:/data \
  --restart unless-stopped \
  ghcr.io/camsoper/shark2mqtt:latest
```

Or with Docker Compose (no `build` needed):

```yaml
services:
  shark2mqtt:
    image: ghcr.io/camsoper/shark2mqtt:latest
    env_file: .env
    volumes:
      - shark2mqtt_data:/data
    restart: unless-stopped

volumes:
  shark2mqtt_data:
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `SHARK_USERNAME` | Yes | | Shark account email |
| `SHARK_PASSWORD` | Yes | | Shark account password |
| `MQTT_HOST` | Yes | | MQTT broker hostname |
| `SHARK_REGION` | No | `us` | `us` or `eu` |
| `SHARK_HOUSEHOLD_ID` | No | Auto-discovered | SharkNinja household ID |
| `MQTT_PORT` | No | `1883` | MQTT broker port |
| `MQTT_USERNAME` | No | | MQTT broker username |
| `MQTT_PASSWORD` | No | | MQTT broker password |
| `MQTT_PREFIX` | No | `shark2mqtt` | MQTT topic prefix |
| `POLL_INTERVAL` | No | `300` | Polling interval in seconds |
| `POLL_INTERVAL_ACTIVE` | No | `20` | Polling interval while cleaning |
| `TOKEN_DIR` | No | `/data` | Directory for persisted auth tokens |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

See [`config.example.env`](config.example.env) for a ready-to-edit template.

## Authentication

shark2mqtt authenticates to SharkNinja's cloud using a browser-based Auth0 flow. The container runs a headed Chromium browser inside a virtual display (`xvfb`) to complete login automatically.

Auth tokens are persisted to the `/data` volume so the browser flow only runs when tokens expire.

To test authentication without starting the full service:

```bash
docker compose run --rm shark2mqtt --auth-once
```

This authenticates, lists discovered vacuums, saves tokens, and exits.

## Home Assistant Entities

Each vacuum is automatically discovered by Home Assistant with the following entities:

| Entity | Type | Description |
|---|---|---|
| `vacuum.<name>` | Vacuum | Main entity with start/stop/pause/return/locate/fan speed |
| `sensor.<name>_battery` | Sensor | Battery level (%) |
| `sensor.<name>_rssi` | Sensor | WiFi signal strength (dBm) |
| `sensor.<name>_error_text` | Sensor | Current error description |
| `binary_sensor.<name>_charging` | Binary Sensor | Charging state |
| `binary_sensor.<name>_error` | Binary Sensor | Error state (on when error present) |

An error device trigger fires when a new error is detected, usable in HA automations.

### Vacuum States

| State | Description |
|---|---|
| `cleaning` | Vacuuming, mopping, or exploring |
| `paused` | Paused mid-clean |
| `returning` | Returning to dock |
| `docked` | On dock, idle or charging |
| `idle` | Stopped, not docked |
| `error` | Error detected |

### Fan Speeds

`eco`, `normal`, `max` — set via the fan speed control on the vacuum card.

## Commands

Standard vacuum commands (start, stop, pause, return to base, locate) work through the Home Assistant vacuum card.

For room-specific cleaning, use the `vacuum.send_command` service:

### Clean a Single Room

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.shark_robot
data:
  command: clean_room
  params:
    room: "Kitchen"
```

### Clean Multiple Rooms

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.shark_robot
data:
  command: clean_rooms
  params:
    rooms: ["Kitchen", "Living Room"]
    clean_type: "dry"       # optional, default: dry
    clean_count: 1          # optional, default: 1
```

### Deep Clean (Matrix Clean)

Two-pass UltraClean mode:

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.shark_robot
data:
  command: matrix_clean
  params:
    room: "Kitchen"
```

## Contributing

This project scratches a personal itch — I'm sharing it in case it helps others, not looking to take on a maintenance burden. If something doesn't work for you, please submit a **pull request** rather than an issue. I only own two vacuum models, so I can't test or troubleshoot devices I don't have. PRs with fixes or support for additional models are welcome; issues requesting changes are likely to be closed.

## Building from Source

```bash
git clone https://github.com/CamSoper/shark2mqtt.git
cd shark2mqtt
docker build -t shark2mqtt .
```

The image is ~1.2 GB due to the bundled Chromium browser required for authentication.
