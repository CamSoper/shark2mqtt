# CLAUDE.md — shark2mqtt

## What This Is

Standalone Python service bridging SharkNinja robot vacuums to Home Assistant via MQTT autodiscovery. Auth → cloud API → MQTT.

## How It Works

1. **Auth**: Patchright (undetected Playwright fork) launches **headed** Chromium via `xvfb-run` to log into Auth0. Cloudflare Turnstile auto-passes in headed mode (blocks headless). CDP `Network.requestWillBeSent` captures the custom-scheme redirect. Tokens persisted to disk.
2. **Device API**: REST calls to `stakra.slatra.thor.skegox.com`. Bearer token + API key. Request signatures are required headers but **NOT validated** server-side — random hex strings are accepted.
3. **Room data**: Preferred from skegox shadow `Robot_Room_List` (format: `FloorID:Room1:Room2:...`), falls back to Ayla `GET_Robot_Room_List` for devices whose skegox room list is empty (rooms configured before skegox migration). Skegox data is checked each poll cycle; Ayla data is fetched once at startup.
4. **MQTT**: HA autodiscovery for vacuum, battery, RSSI, charging, and error entities. Commands via `vacuum.send_command`.

## Non-Obvious Implementation Details

**Auth redirect capture**: Playwright route interception and response handlers do NOT catch Auth0's 302 chain to `com.sharkninja.shark://`. The ONLY working method is CDP `Network.requestWillBeSent`.

**Signatures are decorative**: The `x-iotn-request-signature` header must exist with valid format but the value is not checked. Do not waste time reproducing the signing algorithm.

**Skegox property names**: Skegox shadow uses bare names (`Operating_Mode`), Ayla uses `GET_`/`SET_` prefixes. `SharkVacuum.from_skegox()` adds the `GET_` prefix to match the constants.

**Per-model clean commands**: Devices with `AreasToClean_V3` in their shadow (e.g., UR2360EEUS) use the V3 dict format. Devices without it (e.g., UR250BEXUS) use `Areas_To_Clean` with list format (`["Mode:Room"]`) plus `Operating_Mode: 2`. Detected automatically from shadow properties.

## If Message Signing Starts Being Enforced

`@CamSoper` has notes. Ask him for them.
