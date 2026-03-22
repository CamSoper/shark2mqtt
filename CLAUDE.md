# CLAUDE.md — shark2mqtt

## What This Is

Standalone Python service bridging SharkNinja robot vacuums to Home Assistant via MQTT autodiscovery. Auth → cloud API → MQTT.

## How It Works

1. **Auth**: Patchright (undetected Playwright fork) launches **headed** Chromium via `xvfb-run` to log into Auth0. Cloudflare Turnstile auto-passes in headed mode (blocks headless). CDP `Network.requestWillBeSent` captures the custom-scheme redirect. Tokens persisted to disk.
2. **Device API**: REST calls to `stakra.slatra.thor.skegox.com`. Bearer token + API key. Request signatures are required headers but **NOT validated** server-side — random hex strings are accepted.
3. **Room data**: Fetched from legacy Ayla API on startup — only source for room names. Not in skegox.
4. **MQTT**: HA autodiscovery for vacuum, battery, RSSI, charging, and error entities. Commands via `vacuum.send_command`.

## Non-Obvious Implementation Details

**Auth redirect capture**: Playwright route interception and response handlers do NOT catch Auth0's 302 chain to `com.sharkninja.shark://`. The ONLY working method is CDP `Network.requestWillBeSent`.

**Signatures are decorative**: The `x-iotn-request-signature` header must exist with valid format but the value is not checked. Do not waste time reproducing the signing algorithm.

**Skegox property names**: Skegox shadow uses bare names (`Operating_Mode`), Ayla uses `GET_`/`SET_` prefixes. `SharkVacuum.from_skegox()` adds the `GET_` prefix to match the constants.

**Room data from Ayla only**: Skegox doesn't expose room names. They come from the Ayla `GET_Robot_Room_List` property (format: `FloorID:Room1:Room2:...`).

## If Signing Starts Being Enforced

The `x7k9p2m` hash algorithm is fully cracked with 25 test vectors — documented in the Notion technical reference page ("shark2mqtt — Technical Reference"). The missing piece is the per-request HMAC key derivation. Next step: Frida on Android emulator to hook native HMAC output.
