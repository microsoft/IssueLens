"""Validated configuration for the IssueLens GitHub MCP server."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


_VAULT_HOST_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net$"
)
_SECRET_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,127}$")


class ConfigurationError(RuntimeError):
    """Raised when required server configuration is missing or unsafe."""


@dataclass(frozen=True)
class KeyVaultSecret:
    vault_url: str
    secret_name: str
    secret_version: str | None


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key_secret_uri: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "GitHubAppConfig":
        app_id = environment.get("GITHUB_APP_ID", "").strip()
        secret_uri = environment.get(
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI", ""
        ).strip()
        if not app_id.isdecimal():
            raise ConfigurationError("GITHUB_APP_ID must be a numeric App ID")
        if not secret_uri:
            raise ConfigurationError(
                "GITHUB_APP_PRIVATE_KEY_SECRET_URI is required"
            )
        parse_key_vault_secret_uri(secret_uri)
        return cls(app_id=app_id, private_key_secret_uri=secret_uri)


def parse_key_vault_secret_uri(secret_uri: str) -> KeyVaultSecret:
    """Parse a versioned or unversioned Azure Key Vault secret URI."""
    try:
        parsed = urlparse(secret_uri)
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError("Invalid Key Vault secret URI") from error

    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not _VAULT_HOST_PATTERN.fullmatch(host)
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
        or len(parts) not in (2, 3)
        or parts[0] != "secrets"
        or any(
            not _SECRET_COMPONENT_PATTERN.fullmatch(part)
            for part in parts[1:]
        )
    ):
        raise ConfigurationError(
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI must be an Azure Key Vault "
            "secret URI"
        )
    return KeyVaultSecret(
        vault_url=f"https://{host}",
        secret_name=parts[1],
        secret_version=parts[2] if len(parts) == 3 else None,
    )
