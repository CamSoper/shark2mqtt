# CLAUDE.md — shark2mqtt

## What This Is
A standalone Python service that bridges SharkNinja robot vacuums to Home Assistant via MQTT autodiscovery. Authenticates to SharkNinja's cloud API, polls device state, publishes HA entities, and accepts commands.

## How It Works
1. **Auth**: Patchright (undetected Playwright fork) launches headed Chromium via `xvfb-run` to log into Auth0 (`login.sharkninja.com`). Cloudflare Turnstile auto-passes in headed mode. CDP `Network.requestWillBeSent` captures the custom-scheme redirect. Tokens are persisted to `{TOKEN_DIR}/shark2mqtt_tokens.json`.
2. **Device API**: REST calls to `stakra.slatra.thor.skegox.com` (SharkNinja's AWS-backed backend). Bearer token + API key auth. Request signatures are required headers but NOT validated server-side.
3. **Room data**: Fetched from legacy Ayla API on startup (only source for room names).
4. **MQTT**: Publishes HA autodiscovery for vacuum entities, battery/RSSI sensors, error sensors, and charging binary sensors. Listens for commands.

## Key Files
- `src/shark_auth.py` — Auth0 browser login, token persistence, circuit breaker
- `src/skegox_api.py` — SharkNinja cloud REST client (device state + commands)
- `src/shark_device.py` — Device model, maps API response to HA state
- `src/mqtt_client.py` — MQTT connection, HA autodiscovery, command dispatch
- `src/main.py` — Entry point, polling loop, signal handling
- `src/ayla_api.py` — Legacy Ayla API (used only for room data on startup)
- `src/config.py` — Pydantic settings from env vars
- `src/const.py` — Constants, enums, property name mappings

## Running
```bash
# First time — browser auth, save tokens
xvfb-run --auto-servernum python -m src.main --auth-once

# Normal operation
xvfb-run --auto-servernum python -m src.main

# Without xvfb-run (if DISPLAY is available)
python -m src.main
```

## Configuration
Minimum `.env`:
```
SHARK_USERNAME=your@email.com
SHARK_PASSWORD=your_password
MQTT_HOST=192.168.x.x
MQTT_PORT=1883
MQTT_USERNAME=mqtt_user
MQTT_PASSWORD=mqtt_pass
```

Everything else (household ID, device IDs, rooms, floor IDs) is auto-discovered.

## Docker
- Base: `python:3.12-slim-bookworm`
- Needs: Patchright Chromium + xvfb + xauth
- Entrypoint: `xvfb-run --auto-servernum python -m src.main`
- Non-root user `shark`
- Volume `/data` for token persistence
- Chromium flags: `--no-sandbox --disable-setuid-sandbox --disable-gpu`
- Must use full Chromium (not headless shell) — Patchright's `_find_chromium()` locates it

## Critical Implementation Details

### Auth redirect capture
Playwright route interception and response handlers do NOT catch Auth0's 302 chain to the custom scheme `com.sharkninja.shark://`. The ONLY working method is CDP `Network.requestWillBeSent` which sees the redirect URL before Chromium aborts the navigation.

### Signature headers are decorative
The `x-iotn-request-signature` header must be present with valid format but the server does not validate the signature value. Random hex strings are accepted. Do not waste time trying to reproduce the signing algorithm.

### Skegox property names
The skegox shadow uses bare property names (`Operating_Mode`, `Battery_Capacity`) without the `GET_`/`SET_` prefixes that Ayla uses. The `SharkVacuum.from_skegox()` factory method adds the `GET_` prefix to match the constants used by the property accessors.

### Room data comes from Ayla only
The skegox API does not expose room names. They're fetched from the legacy Ayla API (`GET_Robot_Room_List` property, format: `FloorID:Room1:Room2:...`). If Ayla goes offline, rooms won't be available.

### Active polling
Poll interval drops from 300s to 20s when any vacuum has `ha_state == "cleaning"`, matching the vacuum's own undocked report rate.

## API Constants
- **Skegox base**: `https://stakra.slatra.thor.skegox.com`
- **API key**: `QQdbSrgicK2PxvACI1a2P5AN2xgO78Lw1VvnYczb`
- **Household discovery**: `GET /householdsEndUser?userId={uid}`
- **Auth0 client ID**: `wsguxrqm77mq4LtrTrwg8ZJUxmSrexGi` (US)
