"""Repository-scoped GitHub App authentication and issue-triage operations."""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import os
import pathlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import jwt


_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REFRESH_MARGIN_SECONDS = 300
_MAX_ISSUE_IMAGES = 5
_MAX_ISSUE_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_ISSUE_IMAGES_TOTAL_BYTES = 15 * 1024 * 1024
_MAX_IMAGE_REDIRECTS = 4
_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_URL_PATTERN = re.compile(r'https://[^\s<>"\')\]]+')
_GITHUB_ASSET_HOSTS = {
    "github.com",
    "private-user-images.githubusercontent.com",
    "user-images.githubusercontent.com",
}
_GITHUB_REDIRECT_HOST_PATTERN = re.compile(
    r"^github-production-user-asset-[0-9]+\.s3\.amazonaws\.com$"
)
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


class RequestTokenProvider:
    """Provide one request-scoped GitHub token without exposing it to the model."""

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise GitHubAppError("A request-scoped GitHub token is required")
        self._token = token

    async def get_installation_token(
        self, repository: str
    ) -> InstallationCredential:
        _validate_repository(repository)
        return InstallationCredential(
            installation_id=0,
            token=self._token,
            expires_at=float("inf"),
        )


def _validate_repository(repository: str) -> str:
    repository = repository.strip()
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubAppError("Repository must use the owner/repository format")
    return repository


def _extract_issue_image_urls(body: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(body):
        url = html.unescape(match.group(0)).rstrip(".,;:")
        if url in seen or not _is_allowed_image_url(url, allow_redirect=False):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _is_allowed_image_url(url: str, *, allow_redirect: bool) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port is not None
    ):
        return False

    host = parsed.hostname.lower()
    if host == "github.com":
        return parsed.path.startswith("/user-attachments/assets/")
    if host in {
        "private-user-images.githubusercontent.com",
        "user-images.githubusercontent.com",
    }:
        return True
    return bool(
        allow_redirect
        and (
            host == "objects.githubusercontent.com"
            or _GITHUB_REDIRECT_HOST_PATTERN.fullmatch(host)
        )
    )


def _image_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


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
        if operation == "get-issue-images":
            return await self._get_issue_images(
                repository, _require_issue_number(issue_number)
            )

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

    async def _get_issue_images(
        self, repository: str, issue_number: int
    ) -> dict[str, Any]:
        credential = await self._token_provider.get_installation_token(repository)
        api_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential.token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=30, follow_redirects=False
            ) as client:
                response = await client.get(
                    f"{_API_ROOT}/repos/{repository}/issues/{issue_number}",
                    headers=api_headers,
                )
                response.raise_for_status()
                payload = response.json()
                body = payload.get("body") if isinstance(payload, dict) else None
                urls = _extract_issue_image_urls(body if isinstance(body, str) else "")

                images: list[dict[str, str]] = []
                total_bytes = 0
                candidates = urls[:_MAX_ISSUE_IMAGES]
                for index, url in enumerate(candidates, start=1):
                    remaining_bytes = _MAX_ISSUE_IMAGES_TOTAL_BYTES - total_bytes
                    if remaining_bytes <= 0:
                        break
                    try:
                        image = await self._download_issue_image(
                            client,
                            url,
                            credential.token,
                            min(_MAX_ISSUE_IMAGE_BYTES, remaining_bytes),
                        )
                    except GitHubAppError:
                        continue
                    total_bytes += image.pop("byte_count")
                    image["description"] = (
                        f"Image {index} from {repository}#{issue_number} issue body"
                    )
                    images.append(image)
        except httpx.HTTPStatusError as exc:
            raise GitHubAppError(
                f"GitHub API returned HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubAppError("GitHub issue image request failed") from exc

        return {
            "images": images,
            "discovered_count": len(urls),
            "skipped_count": len(urls) - len(images),
        }

    @staticmethod
    async def _download_issue_image(
        client: httpx.AsyncClient,
        url: str,
        token: str,
        maximum_bytes: int,
    ) -> dict[str, Any]:
        current_url = url
        for _ in range(_MAX_IMAGE_REDIRECTS + 1):
            if not _is_allowed_image_url(current_url, allow_redirect=True):
                raise GitHubAppError("GitHub issue image host is not allowed")

            host = urlparse(current_url).hostname
            headers = {"Accept": "image/png,image/jpeg,image/gif,image/webp"}
            if host in _GITHUB_ASSET_HOSTS:
                headers["Authorization"] = f"Bearer {token}"

            try:
                request = client.build_request("GET", current_url, headers=headers)
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise GitHubAppError("GitHub issue image download failed") from exc

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise GitHubAppError("GitHub issue image redirect is invalid")
                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if media_type not in _IMAGE_MEDIA_TYPES:
                    raise GitHubAppError("GitHub issue attachment is not a supported image")

                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > maximum_bytes:
                    raise GitHubAppError("GitHub issue image exceeds the size limit")

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum_bytes:
                        raise GitHubAppError("GitHub issue image exceeds the size limit")
            except (httpx.HTTPError, ValueError) as exc:
                raise GitHubAppError("GitHub issue image download failed") from exc
            finally:
                await response.aclose()

            if not content:
                raise GitHubAppError("GitHub issue image is empty")
            if _image_media_type(content) != media_type:
                raise GitHubAppError(
                    "GitHub issue attachment content does not match its image type"
                )
            return {
                "data": base64.b64encode(content).decode("ascii"),
                "mime_type": media_type,
                "byte_count": len(content),
            }

        raise GitHubAppError("GitHub issue image has too many redirects")


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
