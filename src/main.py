"""shark2mqtt — Shark vacuum to MQTT bridge for Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any

import aiomqtt
import filetype

# Import floor map parsing function
from visualize_floor_map import parse_floor_map_bytes

from .ayla_api import AylaApi, MardData, debug_dump_mard_structure, parse_mard
from .config import Settings
from .exc import SharkAuthError
from .mqtt_client import MqttClient
from .shark_auth import SharkAuth
from .shark_device import SharkVacuum
from .skegox_api import SkegoxApi

# Broker CONNACK codes that mean "your credentials were refused" rather than
# "the broker is unreachable". 4/5 are MQTT 3.1.1 CONNACK codes; 134/135 are
# the MQTT 5 reason codes for the same two conditions.
_MQTT_AUTH_RCS = frozenset({4, 5, 134, 135})


def _is_mqtt_auth_failure(err: aiomqtt.MqttError) -> bool:
    """Whether an MQTT error is a credential rejection.

    aiomqtt carries the code on `rc`, as a bare int on MQTT 3.1.1 and a
    paho ReasonCode (which holds the number in `.value`) on MQTT 5, so the
    number has to be dug out rather than matched in the message text.
    """
    rc = getattr(err, "rc", None)
    return getattr(rc, "value", rc) in _MQTT_AUTH_RCS

logger = logging.getLogger("shark2mqtt")
IMAGE_CACHE_INTERVAL = 30

class CommandRouter:
    """Routes commands to the correct API based on per-device api_backend."""

    def __init__(
        self,
        skegox: SkegoxApi,
        ayla: AylaApi,
        devices: dict[str, SharkVacuum],
    ) -> None:
        self._skegox = skegox
        self._ayla = ayla
        self._devices = devices

    def _api_for(self, device_id: str) -> Any:
        device = self._devices.get(device_id)
        if device and device.api_backend == "ayla":
            return self._ayla
        return self._skegox

    async def send_command(self, device_id: str, command: str) -> None:
        await self._api_for(device_id).send_command(device_id, command)

    async def set_fan_speed(self, device_id: str, speed: str) -> None:
        await self._api_for(device_id).set_fan_speed(device_id, speed)

    async def set_water_flow(self, device_id: str, level: str) -> None:
        await self._api_for(device_id).set_water_flow(device_id, level)

    async def clean_rooms(
        self,
        device_id: str,
        rooms: list[str],
        floor_id: str,
        clean_type: str = "dry",
        clean_count: int = 1,
        mode: str = "UserRoom",
        use_v3: bool = False,
    ) -> None:
        await self._api_for(device_id).clean_rooms(
            device_id, rooms, floor_id, clean_type, clean_count, mode, use_v3,
        )


async def _fetch_skegox_mard(
    api: SkegoxApi, dsn: str, product_name: str,
) -> MardData:
    """Fetch and parse the skegox MARD file for a device.

    Skegox exposes its own MARD file at the property-files endpoint
    (two-hop fetch: wrapper returns a presigned S3 URL). Same schema
    as Ayla MARD but can have a different floor_id and different
    AZ_N -> name mappings on skegox-migrated accounts. See issue #4.
    """
    try:
        body = await api.fetch_property_file(dsn, "MARD")
    except Exception:
        logger.debug("Skegox MARD fetch failed for %s", product_name, exc_info=True)
        return MardData({}, [], None)
    if not body:
        logger.debug(
            "Skegox MARD for %s (%s): not available", product_name, dsn,
        )
        return MardData({}, [], None)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Skegox MARD fetched for %s (%s): %d bytes",
            product_name, dsn, len(body),
        )
        debug_dump_mard_structure(body, product_name, source="Skegox MARD")
    return parse_mard(body, product_name, dsn, source="Skegox MARD")

async def _fetch_skegox_visual_floor(
    api: SkegoxApi,
    dsn: str,
    product_name: str,
) -> bytes:
    """Fetch and retrieve the Visual_Floor_1 bin file for a device.

    This method fetches the Visual_Floor_1 property from Skegox API
    and returns it as bytes.
    """
    source = "Skegox Visual_Floor_1"
    try:
        body = await api.fetch_property_file(dsn, "Visual_Floor_1")
    except Exception as e:
        logger.debug("Skegox Visual_Floor_1 fetch failed for %s: %s", product_name, e, exc_info=True)
        return b""

    if not body:
        logger.debug(
            "Skegox Visual_Floor_1 for %s (%s): not available",
            product_name,
            dsn,
        )
        return b""

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Skegox Visual_Floor_1 fetched for %s (%s): %d bytes",
            product_name,
            dsn,
            len(body),
        )

    try:
        parsed = body.decode("utf-8", errors='replace')
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.debug("%s for %s: not valid JSON", source, product_name)
        return {}

    logger.debug("Decoded Body Type: %s", type(parsed))
    # Optionally, you can add code here to identify the file type
    # For example, checking magic numbers or headers

    return body

async def poll_loop(
    api: SkegoxApi,
    ayla_api: AylaApi,
    mqtt: MqttClient,
    auth: SharkAuth,
    config: Settings,
    devices_map: dict[str, SharkVacuum],
    ayla_room_data: dict[str, tuple[str, list[str]]],
    ayla_mard: dict[str, MardData],
    command_event: asyncio.Event,
) -> None:
    """Periodically poll device state and publish to MQTT."""
    prev_errors: dict[str, int] = {}
    first_poll = True
    # Skegox MARD cache — fetched once per session per device. MARD
    # only changes on room edits/map saves, not per poll, so re-fetching
    # every cycle would waste API calls and S3 presigned URLs.
    skegox_mard_cache: dict[str, MardData] = {}
    floor_map_last_update: dict[str, float] = {}
    shegox_floor_map_bin = None

    while True:
        any_active = False
        try:
            await auth.ensure_authenticated()

            # --- Skegox devices (primary) ---
            skegox_snds: set[str] = set()
            raw_devices = await api.get_all_devices()
            for raw in raw_devices:
                device = SharkVacuum.from_skegox(raw)

                # Room source priority (issue #4):
                # 1. Skegox MARD — authoritative for migrated devices,
                #    has current floor_id and current AZ_N -> name map.
                # 2. Ayla MARD — authoritative for pure-Ayla accounts.
                #    Stale on migrated accounts (frozen at migration).
                # 3. Ayla Robot_Room_List fallback (shadow-parsed).
                # 4. Skegox Robot_Room_List (already on device from
                #    from_skegox, left as-is when nothing else applies).
                skegox_mard = skegox_mard_cache.get(device.dsn)
                if skegox_mard is None:
                    skegox_mard = await _fetch_skegox_mard(
                        api, device.dsn, device.product_name,
                    )
                    # Cache only on success so a transient failure can be
                    # retried next poll.
                    if skegox_mard.rooms:
                        skegox_mard_cache[device.dsn] = skegox_mard
                ayla = ayla_mard.get(device.dsn)

                # Check if we should update the floor map
                # Always update on first fetch, otherwise only if cleaning and cache interval has passed
                last_update = floor_map_last_update.get(device.dsn, 0.0)
                is_cleaning = device.ha_state == "cleaning"
                should_update_map = (
                    shegox_floor_map_bin is None or  # First time, always fetch
                    (is_cleaning and (time.time() - last_update) >= IMAGE_CACHE_INTERVAL)
                )

                if should_update_map:
                    visual_floor_data = await _fetch_skegox_visual_floor(api, device.dsn, device.product_name)
                    if visual_floor_data:
                        image_type = filetype.guess_mime(visual_floor_data) or type(visual_floor_data)
                        with Path.open("Visual_Floor_1.bin", "wb") as fp:
                            fp.write(visual_floor_data)
                        logger.info(f"Visual_Floor_1 for {device.product_name} ({device.dsn}): type is {image_type}")

                        # Parse and publish the floor map image
                        try:
                            parsed_map = parse_floor_map_bytes(visual_floor_data)
                            await mqtt.publish_map_image(device, parsed_map)
                            logger.info(f"Published floor map image for {device.product_name}")
                            # Update the cache timestamp on successful publish
                            floor_map_last_update[device.dsn] = time.time()
                        except Exception as e:
                            logger.error(f"Failed to parse/publish floor map: {e}")
                    shegox_floor_map_bin = visual_floor_data or {}

                if skegox_mard.rooms:
                    if first_poll:
                        logger.info(
                            "Using Skegox MARD rooms for %s (%s): %s "
                            "(floor_id=%s)",
                            device.product_name, device.dsn,
                            skegox_mard.rooms, skegox_mard.floor_id,
                        )
                    device.rooms = skegox_mard.rooms
                    device.room_name_map = skegox_mard.name_map
                    if skegox_mard.floor_id:
                        device.floor_id = skegox_mard.floor_id
                elif ayla and ayla.rooms:
                    if first_poll:
                        logger.info(
                            "Using Ayla MARD rooms for %s (%s): %s "
                            "(floor_id=%s)",
                            device.product_name, device.dsn,
                            ayla.rooms, ayla.floor_id,
                        )
                    device.rooms = ayla.rooms
                    device.room_name_map = ayla.name_map
                    if ayla.floor_id:
                        device.floor_id = ayla.floor_id
                elif not device.rooms and device.dsn in ayla_room_data:
                    device.floor_id, device.rooms = ayla_room_data[device.dsn]
                    if first_poll:
                        logger.info(
                            "Using Ayla Robot_Room_List fallback for "
                            "%s (%s): %s (floor_id=%s)",
                            device.product_name, device.dsn,
                            device.rooms, device.floor_id,
                        )

                skegox_snds.add(device.dsn)
                devices_map[device.dsn] = device
                await mqtt.publish_discovery(device)
                await mqtt.publish_state(device, prev_error=prev_errors)
                prev_errors[device.dsn] = device.error_code
                if device.ha_state != "docked":
                    any_active = True

            # --- Ayla fallback (only when skegox has no devices) ---
            # If a user reports missing devices, check whether skegox
            # returned some devices but not all — that would indicate
            # a mixed migration we don't currently handle.
            if first_poll:
                skegox_names = [d.product_name for d in devices_map.values()]
                logger.info(
                    "Skegox returned %d device(s): %s",
                    len(skegox_snds), skegox_names or "(none)",
                )
            if skegox_snds:
                if first_poll:
                    logger.info("Using skegox API, skipping Ayla")
            else:
                try:
                    ayla_devices = await ayla_api.get_devices()
                    if first_poll:
                        ayla_names = [d.product_name for d in ayla_devices]
                        logger.info(
                            "Falling back to Ayla, found %d device(s): %s",
                            len(ayla_devices), ayla_names or "(none)",
                        )
                    for device in ayla_devices:
                        devices_map[device.dsn] = device
                        await mqtt.publish_discovery(device)
                        await mqtt.publish_state(device, prev_error=prev_errors)
                        prev_errors[device.dsn] = device.error_code
                        if device.ha_state != "docked":
                            any_active = True
                except Exception:
                    logger.warning("Ayla device fetch failed", exc_info=True)

            first_poll = False

        except SharkAuthError as e:
            logger.error("Auth error during poll: %s", e)
            await mqtt.publish_status({"state": "auth_error", "message": str(e)})
            await mqtt.publish_unavailable(list(devices_map.values()))
        except Exception:
            logger.exception("Poll cycle failed")
            await mqtt.publish_unavailable(list(devices_map.values()))

        interval = config.poll_interval_active if any_active else config.poll_interval
        try:
            await asyncio.wait_for(command_event.wait(), timeout=interval)
            command_event.clear()
            logger.debug("Poll triggered early by command, waiting for device to update")
            await asyncio.sleep(5)
        except TimeoutError:
            pass


async def run(config: Settings) -> None:
    """Main run loop."""
    auth = SharkAuth(config)
    mqtt = MqttClient(config)

    # --auth-once: authenticate, save tokens, exit
    if config.auth_once:
        logger.info("Running in --auth-once mode")
        await auth.ensure_authenticated()
        if auth.id_token:
            api = SkegoxApi(config, auth)
            ayla_api = AylaApi(config, auth)
            if config.shark_household_id:
                api.set_household(config.shark_household_id)
            skegox_devices = await api.get_all_devices()

            all_devices: list[SharkVacuum] = []
            for d in skegox_devices:
                all_devices.append(SharkVacuum.from_skegox(d))

            # Fall back to Ayla only when skegox has no devices
            if not all_devices:
                all_devices = await ayla_api.get_devices()

            logger.info(
                "Auth successful. Found %d device(s). Tokens saved.",
                len(all_devices),
            )
            for v in all_devices:
                logger.info(
                    "  %s (%s) [%s]: battery=%d%%",
                    v.product_name, v.dsn, v.api_backend, v.battery_level,
                )
            await api.close()
            await ayla_api.close()
        else:
            logger.error("Authentication failed — no id_token obtained")
        return

    api = SkegoxApi(config, auth)
    ayla_api = AylaApi(config, auth)
    if config.shark_household_id:
        api.set_household(config.shark_household_id)

    # Shared mutable device map for command handler
    devices_map: dict[str, SharkVacuum] = {}

    # Set up graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    router = CommandRouter(api, ayla_api, devices_map)

    try:
        await auth.ensure_authenticated()

        # Prefetch Ayla room data once at startup. MARD
        # (Mobile_App_Room_Definition) is the authoritative source for
        # room names and floor_id; Robot_Room_List from either API may
        # contain phantoms or placeholders. See issue #4.
        ayla_room_data: dict[str, tuple[str, list[str]]] = {}
        ayla_mard: dict[str, MardData] = {}
        try:
            ayla_vacuums = await ayla_api.get_devices()
            for v in ayla_vacuums:
                bsn = v._properties.get("GET_Battery_Serial_Num", "")
                snd = bsn.split("-")[-1] if "-" in bsn else ""
                if snd and v.rooms:
                    ayla_room_data[snd] = (v.floor_id, v.rooms)
                    logger.info("Ayla room fallback for %s: %s", snd, v.rooms)
                if snd and (v.room_name_map or v.rooms):
                    ayla_mard[snd] = MardData(
                        v.room_name_map or {},
                        v.rooms,
                        v.floor_id or None,
                    )
        except Exception:
            logger.warning("Failed to prefetch Ayla room data", exc_info=True)

        async with mqtt:
            await mqtt.publish_status({"state": "online"})

            command_event = asyncio.Event()

            async with asyncio.TaskGroup() as tg:
                tg.create_task(poll_loop(api, ayla_api, mqtt, auth, config, devices_map, ayla_room_data, ayla_mard, command_event))
                tg.create_task(mqtt.command_listener(router, devices_map, command_event))

                async def _shutdown_watcher() -> None:
                    await stop_event.wait()
                    logger.info("Shutdown signal received")
                    raise SystemExit(0)

                tg.create_task(_shutdown_watcher())

    except (SystemExit, KeyboardInterrupt):
        logger.info("Shutting down gracefully")
    except aiomqtt.MqttError as e:
        # A broker rejection used to surface as a bare aiomqtt traceback,
        # which reads like a shark2mqtt crash even though authentication
        # to SharkNinja succeeded and only the MQTT hop failed (issue #40).
        logger.error(
            "MQTT broker error (%s:%s): %s",
            config.mqtt_host, config.mqtt_port, e,
        )
        if _is_mqtt_auth_failure(e):
            logger.error(
                "The broker rejected the connection as unauthorized. "
                "MQTT_USERNAME is %s. If the broker requires a login, "
                "anonymous connections are refused — set MQTT_USERNAME and "
                "MQTT_PASSWORD to match a broker account.",
                f"set to {config.mqtt_username!r}"
                if config.mqtt_username else "not set (connecting anonymously)",
            )
        raise SystemExit(1) from e
    finally:
        await api.close()
        await ayla_api.close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="shark2mqtt — Shark vacuum to MQTT bridge"
    )
    parser.add_argument(
        "--auth-once",
        action="store_true",
        help="Authenticate once, save tokens, and exit",
    )
    args = parser.parse_args()

    config = Settings()  # type: ignore[call-arg]

    if args.auth_once:
        config.auth_once = True

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("shark2mqtt starting")
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
