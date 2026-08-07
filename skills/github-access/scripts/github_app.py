"""Repository-scoped GitHub App authentication and issue-triage operations."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import pathlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt


_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REFRESH_MARGIN_SECONDS = 300
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$"
)


class GitHubAppError(RuntimeError):
    """Raised when GitHub App authentication or an API operation fails."""


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key_secret_uri: str | None = None
    private_key_path: pathlib.Path | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "GitHubAppConfig":
        env = os.environ if environment is None else environment
        app_id = env.get("GITHUB_APP_ID", "").strip()
        secret_uri = env.get("GITHUB_APP_PRIVATE_KEY_SECRET_URI", "").strip()
        key_path = env.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
        if not app_id:
            raise GitHubAppError("GITHUB_APP_ID is not configured")
        if not secret_uri and not key_path:
            raise GitHubAppError(
                "Configure GITHUB_APP_PRIVATE_KEY_SECRET_URI or "
                "GITHUB_APP_PRIVATE_KEY_PATH"
            )
        return cls(
            app_id=app_id,
            private_key_secret_uri=secret_uri or None,
            private_key_path=(
                pathlib.Path(key_path).expanduser()
                if key_path and not secret_uri
                else None
            ),
        )


@dataclass(frozen=True)
class InstallationCredential:
    installation_id: int
    token: str
    expires_at: float


def _validate_repository(repository: str) -> str:
    repository = repository.strip()
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubAppError("Repository must use the owner/repository format")
    return repository


def _parse_secret_uri(secret_uri: str) -> tuple[str, str, str | None]:
    parsed = urlparse(secret_uri)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not parsed.netloc.endswith(".vault.azure.net")
        or len(parts) not in (2, 3)
        or parts[0] != "secrets"
    ):
        raise GitHubAppError(
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI must be an Azure Key Vault "
            "secret URI"
        )
    return (
        f"{parsed.scheme}://{parsed.netloc}",
        parts[1],
        parts[2] if len(parts) == 3 else None,
    )


async def load_private_key(config: GitHubAppConfig) -> str:
    """Load the App PEM from Key Vault when hosted or an ignored local file."""
    if config.private_key_secret_uri:
        vault_url, secret_name, secret_version = _parse_secret_uri(
            config.private_key_secret_uri
        )
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        credential = DefaultAzureCredential()
        try:
            async with SecretClient(vault_url=vault_url, credential=credential) as client:
                secret = await client.get_secret(secret_name, secret_version)
                if not secret.value:
                    raise GitHubAppError("The GitHub App private-key secret is empty")
                return secret.value
        except GitHubAppError:
            raise
        except Exception as exc:
            raise GitHubAppError(
                "Could not read the GitHub App private key from Azure Key Vault"
            ) from exc
        finally:
            await credential.close()

    if config.private_key_path:
        try:
            return await asyncio.to_thread(
                config.private_key_path.read_text, encoding="utf-8"
            )
        except OSError as exc:
            raise GitHubAppError("Could not read GITHUB_APP_PRIVATE_KEY_PATH") from exc

    raise GitHubAppError("No GitHub App private key source is configured")


class GitHubAppTokenProvider:
    """Resolve repositories and cache short-lived credentials by installation."""

    def __init__(
        self,
        config: GitHubAppConfig,
        private_key_loader: Callable[[], Awaitable[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._private_key_loader = private_key_loader or (
            lambda: load_private_key(config)
        )
        self._transport = transport
        self._clock = clock
        self._repository_installations: dict[str, int] = {}
        self._credentials: dict[int, InstallationCredential] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> "GitHubAppTokenProvider":
        return cls(GitHubAppConfig.from_environment())

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=30)

    async def get_installation_token(
        self, repository: str
    ) -> InstallationCredential:
        """Return a cached credential for the App installation covering a repo."""
        repository = _validate_repository(repository)
        normalized = repository.lower()
        async with self._lock:
            now = self._clock()
            installation_id = self._repository_installations.get(normalized)
            if installation_id is not None:
                cached = self._credentials.get(installation_id)
                if cached and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                    return cached

            private_key = await self._private_key_loader()
            try:
                app_jwt = jwt.encode(
                    {
                        "iat": int(now) - 60,
                        "exp": int(now) + 540,
                        "iss": self._config.app_id,
                    },
                    private_key,
                    algorithm="RS256",
                )
            except (jwt.PyJWTError, TypeError, ValueError) as exc:
                raise GitHubAppError("The configured GitHub App key is invalid") from exc

            app_headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": _API_VERSION,
            }
            try:
                async with self._client() as client:
                    if installation_id is None:
                        response = await client.get(
                            f"{_API_ROOT}/repos/{repository}/installation",
                            headers=app_headers,
                        )
                        response.raise_for_status()
                        installation_id = int(response.json()["id"])
                        self._repository_installations[normalized] = installation_id

                    cached = self._credentials.get(installation_id)
                    if cached and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                        return cached

                    response = await client.post(
                        f"{_API_ROOT}/app/installations/"
                        f"{installation_id}/access_tokens",
                        headers=app_headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    credential = InstallationCredential(
                        installation_id=installation_id,
                        token=payload["token"],
                        expires_at=datetime.fromisoformat(
                            payload["expires_at"].replace("Z", "+00:00")
                        ).timestamp(),
                    )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise GitHubAppError(
                    f"The IssueLens GitHub App is not installed for {repository} "
                    "or cannot mint an installation token"
                ) from exc

            if not credential.token:
                raise GitHubAppError("GitHub returned an empty installation token")
            self._credentials[installation_id] = credential
            return credential


class GitHubAppClient:
    """Perform the finite GitHub operations needed by IssueLens."""

    def __init__(
        self,
        token_provider: GitHubAppTokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport

    async def execute(
        self,
        operation: str,
        repository: str,
        *,
        issue_number: int | None = None,
        query: str | None = None,
        path: str | None = None,
        state: str = "open",
        since: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        per_page: int = 30,
    ) -> Any:
        repository = _validate_repository(repository)
        method = "GET"
        url = f"{_API_ROOT}/repos/{repository}"
        params: dict[str, Any] = {}
        body: dict[str, Any] | None = None
        per_page = max(1, min(per_page, 100))

        if operation == "get-repository":
            pass
        elif operation == "list-issues":
            url += "/issues"
            params = {
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
            }
            if since:
                params["since"] = since
        elif operation == "get-issue":
            url += f"/issues/{_require_issue_number(issue_number)}"
        elif operation == "list-comments":
            url += f"/issues/{_require_issue_number(issue_number)}/comments"
            params = {"per_page": per_page}
        elif operation == "list-reactions":
            url += f"/issues/{_require_issue_number(issue_number)}/reactions"
            params = {"per_page": per_page}
        elif operation == "search-issues":
            if not query or not query.strip():
                raise GitHubAppError("search-issues requires a query")
            url = f"{_API_ROOT}/search/issues"
            params = {
                "q": f"repo:{repository} is:issue {query.strip()}",
                "per_page": per_page,
            }
        elif operation == "list-labels":
            url += "/labels"
            params = {"per_page": per_page}
        elif operation == "get-file":
            if (
                not path
                or path.startswith("/")
                or "\\" in path
                or ".." in pathlib.PurePosixPath(path).parts
            ):
                raise GitHubAppError("get-file requires a repository-relative path")
            url += f"/contents/{path}"
        elif operation == "add-labels":
            method = "POST"
            url += f"/issues/{_require_issue_number(issue_number)}/labels"
            body = {"labels": _require_names(labels, "labels")}
        elif operation == "set-assignees":
            method = "PATCH"
            url += f"/issues/{_require_issue_number(issue_number)}"
            body = {"assignees": _require_names(assignees, "assignees")}
        else:
            raise GitHubAppError(f"Unsupported GitHub operation: {operation}")

        credential = await self._token_provider.get_installation_token(repository)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential.token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=30
            ) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, json=body
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:500]
            raise GitHubAppError(f"GitHub API returned HTTP {status}: {detail}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubAppError("GitHub API request failed") from exc

        if operation == "list-issues":
            return [item for item in payload if "pull_request" not in item]
        if operation == "get-file" and isinstance(payload, dict):
            if payload.get("encoding") == "base64" and isinstance(payload.get("content"), str):
                payload["decoded_content"] = base64.b64decode(
                    payload["content"], validate=False
                ).decode("utf-8", errors="replace")
                payload.pop("content", None)
        return payload


def _require_issue_number(issue_number: int | None) -> int:
    if not isinstance(issue_number, int) or issue_number < 1:
        raise GitHubAppError("This operation requires a positive issue_number")
    return issue_number


def _require_names(names: list[str] | None, field: str) -> list[str]:
    cleaned = [
        name.strip()
        for name in names or []
        if isinstance(name, str) and name.strip()
    ]
    if not cleaned:
        raise GitHubAppError(f"This operation requires one or more {field}")
    return cleaned


async def _diagnose(repository: str) -> int:
    provider = GitHubAppTokenProvider.from_environment()
    credential = await provider.get_installation_token(repository)
    print(json.dumps({
        "repository": repository,
        "installation_id": credential.installation_id,
        "expires_at": datetime.fromtimestamp(
            credential.expires_at
        ).astimezone().isoformat(),
        "token_exposed": False,
    }))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify GitHub App installation-token access without printing the token."
    )
    parser.add_argument("repository", help="Target repository as owner/repository")
    args = parser.parse_args()
    try:
        return asyncio.run(_diagnose(args.repository))
    except GitHubAppError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
