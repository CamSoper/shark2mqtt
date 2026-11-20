# AGENTS.md — shark2mqtt

Standing orders for anyone (human or agent) changing this repo. Repo internals are in `CLAUDE.md`.

## Don't break the Home Assistant add-on

[400HPMustang/shark2mqtt-addon](https://github.com/400HPMustang/shark2mqtt-addon) wraps this project's published image for Home Assistant OS users. A lot of people run shark2mqtt only through it, and it's maintained by someone else, so a change here that breaks it breaks them with no warning. **Every PR and release has to keep the add-on working.** Check this list before merging, and say in the PR how the change affects the add-on (usually "it doesn't").

How the add-on uses us, and what that pins down:

- **The image stays public on GHCR.** It builds `FROM ghcr.io/camsoper/shark2mqtt:<X.Y.Z>` with no registry credentials. Never make the `shark2mqtt` package private, even if other `camsoper` packages go private.
- **Tag scheme and platforms.** Releases are git tags `vX.Y.Z` (stable, not draft or prerelease); `docker-publish.yml` pushes the image as `X.Y.Z` (no `v`), `X.Y`, `X` and `latest`, for `linux/amd64` and `linux/arm64`. The add-on's release watcher parses `vX.Y.Z` tags and its Dockerfile pins the bare `X.Y.Z` tag. Don't change either format or drop a platform.
- **Image layout.** The add-on replaces our `ENTRYPOINT`, installs `jq` and `curl` with `apt-get`, starts `Xvfb :99` itself, and runs `python -m src.main` from the image's working directory. So keep: a Debian base with `apt-get`, `Xvfb` on `PATH`, `WORKDIR /app`, and `src.main` as the entry module.
- **It runs as root.** The add-on switches to `USER root` to read Home Assistant's options file. The app must keep working as root (Chromium is launched with `--no-sandbox` for this reason; don't drop it).
- **Config is env vars only.** The add-on maps its options to env vars (today: Shark login, region, household, MQTT, `POLL_INTERVAL`, `POLL_INTERVAL_ACTIVE`, `LOG_LEVEL`, `AYLA_MERGE`, `MAP_IMAGE`, `MAP_SHOW_SCAN`) and leaves every other setting at its default. So: never rename or remove an env var, never change what an existing default does, and give every new setting a default that keeps today's behavior. Add-on users can't set it until the add-on exposes it.
- **Tokens live in `/data`.** The add-on sets `TOKEN_DIR=/data`; the token file is `shark2mqtt_tokens.json`. Don't rename it or move where it's read from.
- **Entities stay put.** MQTT topics and discovery `unique_id`s are what users' dashboards and automations hang on. Don't change existing ones; add new entities alongside.

If a change genuinely has to break one of these, don't ship it quietly: open an issue on the add-on repo first, call it out at the top of the release notes, and bump the major version.

When a release adds a setting that add-on users would want, mention @400HPMustang on the related issue or PR so it can be exposed in the add-on.
