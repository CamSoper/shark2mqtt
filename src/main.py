"""shark2mqtt — Shark vacuum to MQTT bridge for Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .ayla_api import AylaApi
from .config import Settings
from .exc import SharkAuthError
from .mqtt_client import MqttClient
from .shark_auth import SharkAuth
from .shark_device import SharkVacuum

logger = logging.getLogger("shark2mqtt")


async def poll_loop(
    ayla: AylaApi,
    mqtt: MqttClient,
    auth: SharkAuth,
    config: Settings,
    devices_map: dict[str, SharkVacuum],
) -> None:
    """Periodically poll device state and publish to MQTT."""
    while True:
        try:
            # Refresh auth if needed
            id_token = await auth.ensure_authenticated()
            if ayla.token_expiring_soon:
                await ayla.sign_in(id_token)

            # Fetch all devices with properties
            devices = await ayla.get_devices()

            # Update shared devices map and publish
            for device in devices:
                devices_map[device.dsn] = device
                await mqtt.publish_discovery(device)
                await mqtt.publish_state(device)

        except SharkAuthError as e:
            logger.error("Auth error during poll: %s", e)
            await mqtt.publish_status({"state": "auth_error", "message": str(e)})
            await mqtt.publish_unavailable(list(devices_map.values()))
        except Exception:
            logger.exception("Poll cycle failed")

        await asyncio.sleep(config.poll_interval)


async def token_refresh_loop(auth: SharkAuth, ayla: AylaApi) -> None:
    """Proactively refresh tokens before they expire."""
    while True:
        await asyncio.sleep(60)
        try:
            if ayla.token_expiring_soon:
                await ayla.refresh_auth()
        except SharkAuthError:
            logger.warning("Proactive token refresh failed, will retry at next poll")


async def run(config: Settings) -> None:
    """Main run loop."""
    auth = SharkAuth(config)
    ayla = AylaApi(config, auth)
    mqtt = MqttClient(config)

    # --auth-once: authenticate, save tokens, exit
    if config.auth_once:
        logger.info("Running in --auth-once mode")
        await auth.ensure_authenticated()
        id_token = auth.id_token
        if id_token:
            await ayla.sign_in(id_token)
            devices = await ayla.list_devices()
            logger.info("Auth successful. Found %d device(s). Tokens saved.", len(devices))
        else:
            logger.error("Authentication failed — no id_token obtained")
        await ayla.close()
        return

    # --offline: load cached tokens, skip Auth0
    if config.offline:
        logger.info("Running in --offline mode (no Auth0 calls)")

    # Shared mutable device map for command handler
    devices_map: dict[str, SharkVacuum] = {}

    # Set up graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        # Initial auth + Ayla sign-in
        if not config.offline:
            id_token = await auth.ensure_authenticated()
            await ayla.sign_in(id_token)
        else:
            # In offline mode, try to use cached Ayla tokens directly
            try:
                await ayla.refresh_auth()
            except SharkAuthError:
                logger.error("Offline mode: no valid cached tokens. Run --auth-once first.")
                return

        async with mqtt:
            await mqtt.publish_status({"state": "online"})

            async with asyncio.TaskGroup() as tg:
                tg.create_task(poll_loop(ayla, mqtt, auth, config, devices_map))
                tg.create_task(mqtt.command_listener(ayla, devices_map))
                tg.create_task(token_refresh_loop(auth, ayla))

                # Wait for shutdown signal
                async def _shutdown_watcher() -> None:
                    await stop_event.wait()
                    logger.info("Shutdown signal received")
                    raise SystemExit(0)

                tg.create_task(_shutdown_watcher())

    except (SystemExit, KeyboardInterrupt):
        logger.info("Shutting down gracefully")
    finally:
        await ayla.close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="shark2mqtt — Shark vacuum to MQTT bridge")
    parser.add_argument("--auth-once", action="store_true", help="Authenticate once, save tokens, and exit")
    parser.add_argument("--offline", action="store_true", help="Use cached tokens only, no Auth0 calls")
    args = parser.parse_args()

    config = Settings()  # type: ignore[call-arg]

    # Override config with CLI args
    if args.auth_once:
        config.auth_once = True
    if args.offline:
        config.offline = True

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("shark2mqtt starting")
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
