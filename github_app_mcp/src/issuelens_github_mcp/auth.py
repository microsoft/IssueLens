"""GitHub App authentication with repository-confined installation tokens."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx
import jwt

from .config import GitHubAppConfig, parse_key_vault_secret_uri


_API_ROOT = "https://api.github.com"
_API_VERSION = "2026-03-10"
_REFRESH_MARGIN_SECONDS = 300
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$"
)

PermissionLevel = Literal["read", "write"]
Permissions = Mapping[str, PermissionLevel]
_ALLOWED_PERMISSIONS: dict[str, frozenset[PermissionLevel]] = {
    "contents": frozenset({"read"}),
    "issues": frozenset({"read", "write"}),
    "metadata": frozenset({"read"}),
}


class GitHubAppError(RuntimeError):
    """Raised when App authentication or token minting fails safely."""


@dataclass(frozen=True)
class InstallationCredential:
    installation_id: int
    repository: str
    permissions: tuple[tuple[str, PermissionLevel], ...]
    token: str
    expires_at: float


def validate_repository(repository: str) -> str:
    repository = repository.strip()
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubAppError("Repository must use the owner/repository format")
    return repository


def normalize_permissions(
    permissions: Permissions,
) -> tuple[tuple[str, PermissionLevel], ...]:
    normalized: list[tuple[str, PermissionLevel]] = []
    for name, level in permissions.items():
        if level not in _ALLOWED_PERMISSIONS.get(name, frozenset()):
            raise GitHubAppError(
                f"Unsupported GitHub App token permission: {name}={level}"
            )
        normalized.append((name, level))
    if not normalized:
        raise GitHubAppError("At least one token permission is required")
    return tuple(sorted(normalized))


class KeyVaultPrivateKeyLoader:
    """Load and retain the App private key from Key Vault in process memory."""

    def __init__(self, secret_uri: str) -> None:
        self._secret = parse_key_vault_secret_uri(secret_uri)
        self._private_key: str | None = None
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._private_key is not None:
                return self._private_key

            from azure.identity.aio import DefaultAzureCredential
            from azure.keyvault.secrets.aio import SecretClient

            credential = DefaultAzureCredential()
            try:
                async with SecretClient(
                    vault_url=self._secret.vault_url,
                    credential=credential,
                ) as client:
                    secret = await client.get_secret(
                        self._secret.secret_name,
                        self._secret.secret_version,
                    )
            except Exception as error:
                raise GitHubAppError(
                    "Could not load the GitHub App private key from Key Vault"
                ) from error
            finally:
                await credential.close()

            if not secret.value:
                raise GitHubAppError("The GitHub App private-key secret is empty")
            self._private_key = secret.value
            return self._private_key


class GitHubAppTokenProvider:
    """Mint cached tokens restricted to one repository and permission set."""

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        private_key_loader: Callable[[], Awaitable[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._private_key_loader = private_key_loader or KeyVaultPrivateKeyLoader(
            config.private_key_secret_uri
        )
        self._transport = transport
        self._clock = clock
        self._repository_installations: dict[str, int] = {}
        self._credentials: dict[
            tuple[str, tuple[tuple[str, PermissionLevel], ...]],
            InstallationCredential,
        ] = {}
        self._repository_locks: dict[str, asyncio.Lock] = {}
        self._state_lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=30)

    async def get_token(
        self,
        repository: str,
        permissions: Permissions,
    ) -> InstallationCredential:
        repository = validate_repository(repository)
        normalized_repository = repository.casefold()
        normalized_permissions = normalize_permissions(permissions)
        cache_key = (normalized_repository, normalized_permissions)

        async with self._state_lock:
            now = self._clock()
            cached = self._credentials.get(cache_key)
            if cached and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                return cached
            repository_lock = self._repository_locks.setdefault(
                normalized_repository, asyncio.Lock()
            )

        async with repository_lock:
            now = self._clock()
            async with self._state_lock:
                cached = self._credentials.get(cache_key)
                if cached and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                    return cached

            credential = await self._mint_token(
                repository,
                normalized_repository,
                normalized_permissions,
                now,
            )
            async with self._state_lock:
                self._credentials[cache_key] = credential
            return credential

    async def _mint_token(
        self,
        repository: str,
        normalized_repository: str,
        normalized_permissions: tuple[tuple[str, PermissionLevel], ...],
        now: float,
    ) -> InstallationCredential:
        """Resolve an installation and mint one repository-scoped token."""
        app_jwt = await self._app_jwt(now)
        app_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        for attempt in range(2):
            async with self._state_lock:
                installation_id = self._repository_installations.get(
                    normalized_repository
                )
            try:
                async with self._client() as client:
                    if installation_id is None:
                        response = await client.get(
                            f"{_API_ROOT}/repos/{repository}/installation",
                            headers=app_headers,
                        )
                        response.raise_for_status()
                        installation_id = int(response.json()["id"])
                        async with self._state_lock:
                            self._repository_installations[
                                normalized_repository
                            ] = installation_id

                    response = await client.post(
                        f"{_API_ROOT}/app/installations/{installation_id}/access_tokens",
                        headers=app_headers,
                        json={
                            "repositories": [repository.split("/", 1)[1]],
                            "permissions": dict(normalized_permissions),
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {401, 403, 404} and attempt == 0:
                    async with self._state_lock:
                        self._repository_installations.pop(
                            normalized_repository, None
                        )
                    continue
                raise GitHubAppError(
                    f"The GitHub App cannot access {repository} with the "
                    "required permissions"
                ) from error
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                raise GitHubAppError(
                    f"The GitHub App cannot access {repository} with the "
                    "required permissions"
                ) from error

            try:
                if not isinstance(payload, Mapping):
                    raise TypeError
                token = payload.get("token")
                expires_value = payload.get("expires_at")
                if not isinstance(token, str) or not token:
                    raise ValueError
                if not isinstance(expires_value, str):
                    raise TypeError
                expires_at = datetime.fromisoformat(
                    expires_value.replace("Z", "+00:00")
                ).timestamp()
                if expires_at <= now:
                    raise ValueError
            except (TypeError, ValueError, AttributeError) as error:
                raise GitHubAppError(
                    "GitHub returned an invalid installation token response"
                ) from error

            return InstallationCredential(
                installation_id=installation_id,
                repository=repository,
                permissions=normalized_permissions,
                token=token,
                expires_at=expires_at,
            )

        raise GitHubAppError(
            f"The GitHub App cannot resolve a current installation for {repository}"
        )

    async def _app_jwt(self, now: float) -> str:
        private_key = await self._private_key_loader()
        try:
            return jwt.encode(
                {
                    "iat": int(now) - 60,
                    "exp": int(now) + 540,
                    "iss": self._config.app_id,
                },
                private_key,
                algorithm="RS256",
            )
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise GitHubAppError("The configured GitHub App key is invalid") from error
