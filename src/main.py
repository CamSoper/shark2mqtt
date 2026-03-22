"""shark2mqtt — Shark vacuum to MQTT bridge for Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import Settings
from .exc import SharkAuthError
from .mqtt_client import MqttClient
from .shark_auth import SharkAuth
from .shark_device import SharkVacuum
from .skegox_api import SkegoxApi

logger = logging.getLogger("shark2mqtt")


async def poll_loop(
    api: SkegoxApi,
    mqtt: MqttClient,
    auth: SharkAuth,
    config: Settings,
    devices_map: dict[str, SharkVacuum],
) -> None:
    """Periodically poll device state and publish to MQTT."""
    while True:
        any_active = False
        try:
            await auth.ensure_authenticated()

            raw_devices = await api.get_all_devices()
            for raw in raw_devices:
                device = SharkVacuum.from_skegox(raw)
                devices_map[device.dsn] = device
                await mqtt.publish_discovery(device)
                await mqtt.publish_state(device)
                if device.ha_state == "cleaning":
                    any_active = True

        except SharkAuthError as e:
            logger.error("Auth error during poll: %s", e)
            await mqtt.publish_status({"state": "auth_error", "message": str(e)})
            await mqtt.publish_unavailable(list(devices_map.values()))
        except Exception:
            logger.exception("Poll cycle failed")

        interval = config.poll_interval_active if any_active else config.poll_interval
        await asyncio.sleep(interval)


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
            if config.shark_household_id:
                api.set_household(config.shark_household_id)
                devices = await api.get_all_devices()
                logger.info(
                    "Auth successful. Found %d device(s). Tokens saved.", len(devices)
                )
                for d in devices:
                    v = SharkVacuum.from_skegox(d)
                    logger.info("  %s (%s): battery=%d%%", v.product_name, v.dsn, v.battery_level)
            else:
                logger.info("Auth successful. Tokens saved. Set SHARK_HOUSEHOLD_ID to list devices.")
            await api.close()
        else:
            logger.error("Authentication failed — no id_token obtained")
        return

    # Validate config
    if not config.shark_household_id:
        logger.error("SHARK_HOUSEHOLD_ID is required. Get it from the Shark app HAR capture.")
        return

    api = SkegoxApi(config, auth)
    api.set_household(config.shark_household_id)

    # Shared mutable device map for command handler
    devices_map: dict[str, SharkVacuum] = {}

    # Set up graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await auth.ensure_authenticated()

        async with mqtt:
            await mqtt.publish_status({"state": "online"})

            async with asyncio.TaskGroup() as tg:
                tg.create_task(poll_loop(api, mqtt, auth, config, devices_map))
                tg.create_task(mqtt.command_listener(api, devices_map))

                async def _shutdown_watcher() -> None:
                    await stop_event.wait()
                    logger.info("Shutdown signal received")
                    raise SystemExit(0)

                tg.create_task(_shutdown_watcher())

    except (SystemExit, KeyboardInterrupt):
        logger.info("Shutting down gracefully")
    finally:
        await api.close()


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
