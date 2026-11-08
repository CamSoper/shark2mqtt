"""Tests for configuration precedence when using Docker secrets."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src import config as config_module
from src.config import Settings


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    """Provide Docker-style secret files without host environment interference."""
    monkeypatch.delenv("SHARK_PASSWORD", raising=False)
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    path = tmp_path / "secrets"
    path.mkdir()
    (path / "SHARK_PASSWORD").write_text("secret-shark-password\n")
    (path / "MQTT_PASSWORD").write_text("secret-mqtt-password\n")
    return path


class TestDockerSecretSettings:
    """Existing environment and .env values must override Docker secrets."""

    def test_auto_detects_docker_secrets_directory(self):
        # A Docker/Swarm secrets mount is present before the service starts.
        with patch("src.config.Path.is_dir", return_value=True):
            imported = importlib.reload(config_module)
        try:
            assert imported.Settings.model_config["secrets_dir"] == "/run/secrets"
        finally:
            importlib.reload(config_module)

    def test_reads_existing_credential_names_from_secrets(self, secrets_dir):
        config = Settings(
            _env_file=None,
            _secrets_dir=secrets_dir,
            shark_username="shark@example.com",
            mqtt_host="mqtt.example.com",
        )

        assert config.shark_password == "secret-shark-password"
        assert config.mqtt_password == "secret-mqtt-password"

    def test_environment_variables_take_precedence(self, secrets_dir, monkeypatch):
        monkeypatch.setenv("SHARK_PASSWORD", "environment-shark-password")
        monkeypatch.setenv("MQTT_PASSWORD", "environment-mqtt-password")

        config = Settings(
            _env_file=None,
            _secrets_dir=secrets_dir,
            shark_username="shark@example.com",
            mqtt_host="mqtt.example.com",
        )

        assert config.shark_password == "environment-shark-password"
        assert config.mqtt_password == "environment-mqtt-password"

    def test_env_file_takes_precedence(self, secrets_dir, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "SHARK_PASSWORD=dotenv-shark-password\n"
            "MQTT_PASSWORD=dotenv-mqtt-password\n"
        )

        config = Settings(
            _env_file=env_file,
            _secrets_dir=secrets_dir,
            shark_username="shark@example.com",
            mqtt_host="mqtt.example.com",
        )

        assert config.shark_password == "dotenv-shark-password"
        assert config.mqtt_password == "dotenv-mqtt-password"

    def test_environment_precedes_env_file(self, secrets_dir, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("SHARK_PASSWORD=dotenv-shark-password\n")
        monkeypatch.setenv("SHARK_PASSWORD", "environment-shark-password")

        config = Settings(
            _env_file=env_file,
            _secrets_dir=secrets_dir,
            shark_username="shark@example.com",
            mqtt_host="mqtt.example.com",
        )

        assert config.shark_password == "environment-shark-password"

    def test_mqtt_password_remains_optional(self, secrets_dir):
        (secrets_dir / "MQTT_PASSWORD").unlink()

        config = Settings(
            _env_file=None,
            _secrets_dir=secrets_dir,
            shark_username="shark@example.com",
            mqtt_host="mqtt.example.com",
        )

        assert config.mqtt_password is None

    def test_shark_password_is_still_required(self, secrets_dir):
        (secrets_dir / "SHARK_PASSWORD").unlink()

        with pytest.raises(ValidationError, match="shark_password"):
            Settings(
                _env_file=None,
                _secrets_dir=secrets_dir,
                shark_username="shark@example.com",
                mqtt_host="mqtt.example.com",
            )
