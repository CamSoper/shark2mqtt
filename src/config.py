"""Configuration via environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Shark account
    shark_username: str
    shark_password: str
    shark_region: str = Field(default="us", pattern=r"^(us|eu)$")

    # MQTT broker
    mqtt_host: str
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_prefix: str = "shark2mqtt"

    # SharkNinja cloud (skegox)
    shark_household_id: str | None = None

    # Polling
    poll_interval: int = 300

    # Token persistence
    token_dir: str = "/data"

    # Logging
    log_level: str = "INFO"

    # Operation modes
    auth_once: bool = False
    offline: bool = False

    model_config = {"env_file": ".env", "env_prefix": "", "case_sensitive": False}
