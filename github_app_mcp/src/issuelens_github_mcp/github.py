"""Bounded GitHub operations exposed by the IssueLens MCP server."""

from __future__ import annotations

import base64
import html
import json
import pathlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlparse

import httpx

from .auth import GitHubAppError, GitHubAppTokenProvider, Permissions, validate_repository


_API_ROOT = "https://api.github.com"
_API_VERSION = "2026-03-10"
_MAX_RESULT_BYTES = 100_000
_MAX_HTTP_RESPONSE_BYTES = 128 * 1024
_MAX_FILE_BYTES = 64 * 1024
_MAX_QUERY_CHARS = 512
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
_SEARCH_QUALIFIER = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])[-+]?[A-Za-z][A-Za-z0-9_-]*:"
)
ReactionTarget = Literal[
    "issue",
    "pull_request",
    "issue_comment",
    "pull_request_comment",
]
_REACTION_TARGETS: dict[ReactionTarget, tuple[str, str]] = {
    "issue": ("/issues/{target_id}/reactions", "issues"),
    "pull_request": ("/issues/{target_id}/reactions", "issues"),
    "issue_comment": ("/issues/comments/{target_id}/reactions", "issues"),
    "pull_request_comment": (
        "/pulls/comments/{target_id}/reactions",
        "pull_requests",
    ),
}


class GitHubClient:
    """Perform only the repository operations required by IssueLens."""

    def __init__(
        self,
        token_provider: GitHubAppTokenProvider,
        *,
        writes_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._writes_enabled = writes_enabled
        self._transport = transport

    @property
    def writes_enabled(self) -> bool:
        """Whether this trusted client instance permits GitHub writes."""
        return self._writes_enabled

    async def get_repository(self, repository: str) -> Any:
        """Read repository metadata."""
        return await self._request(
            "GET", repository, "", permissions={"metadata": "read"}
        )

    async def list_issues(
        self,
        repository: str,
        *,
        state: Literal["open", "closed", "all"] = "open",
        since: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> list[Any]:
        """List repository issues by latest update, excluding pull requests."""
        if state not in {"open", "closed", "all"}:
            raise GitHubAppError("state must be open, closed, or all")
        params: dict[str, Any] = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            **_pagination(per_page, page),
        }
        if since is not None:
            params["since"] = _timestamp(since)
        payload = await self._request(
            "GET",
            repository,
            "/issues",
            permissions={"issues": "read"},
            params=params,
        )
        if not isinstance(payload, list):
            raise GitHubAppError("GitHub returned an invalid issues response")
        return [item for item in payload if "pull_request" not in item]

    async def get_issue(self, repository: str, issue_number: int) -> Any:
        """Read one issue."""
        return await self._request(
            "GET",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}",
            permissions={"issues": "read"},
        )

    async def list_issue_comments(
        self,
        repository: str,
        issue_number: int,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List comments on one issue."""
        return await self._request(
            "GET",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}/comments",
            permissions={"issues": "read"},
            params=_pagination(per_page, page),
        )

    async def get_issue_comment(
        self,
        repository: str,
        issue_number: int,
        comment_id: int,
    ) -> dict[str, Any]:
        """Read one comment and verify that it belongs to the target issue."""
        repository = validate_repository(repository)
        issue_number = _positive(issue_number, "issue_number")
        comment_id = _positive(comment_id, "comment_id")
        payload = await self._request(
            "GET",
            repository,
            f"/issues/comments/{comment_id}",
            permissions={"issues": "read"},
        )
        expected_issue_url = (
            f"{_API_ROOT}/repos/{repository}/issues/{issue_number}"
        )
        issue_url = payload.get("issue_url") if isinstance(payload, Mapping) else None
        if (
            not isinstance(issue_url, str)
            or issue_url.casefold() != expected_issue_url.casefold()
        ):
            raise GitHubAppError(
                "GitHub comment does not belong to the requested issue"
            )
        user = payload.get("user")
        body = payload.get("body")
        author_association = payload.get("author_association")
        if (
            payload.get("id") != comment_id
            or not isinstance(body, str)
            or not isinstance(author_association, str)
            or not isinstance(user, Mapping)
            or not isinstance(user.get("login"), str)
            or not user.get("login")
            or not isinstance(user.get("type"), str)
            or not user.get("type")
        ):
            raise GitHubAppError("GitHub returned an invalid issue comment")
        return {
            "id": comment_id,
            "body": body,
            "author_association": author_association,
            "user": {
                "login": user.get("login"),
                "type": user.get("type"),
            },
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "html_url": payload.get("html_url"),
        }

    async def list_issue_reactions(
        self,
        repository: str,
        issue_number: int,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List reactions on one issue for criticality scoring."""
        return await self._request(
            "GET",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}/reactions",
            permissions={"issues": "read"},
            params=_pagination(per_page, page),
        )

    async def search_issues(
        self,
        repository: str,
        query: str,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> list[Any]:
        """Search issues within exactly one repository."""
        repository = self._authorize(repository)
        query = query.strip()
        if not query or len(query) > _MAX_QUERY_CHARS:
            raise GitHubAppError(
                f"query must contain between 1 and {_MAX_QUERY_CHARS} characters"
            )
        if _SEARCH_QUALIFIER.search(query):
            raise GitHubAppError(
                "query cannot contain GitHub search qualifiers"
            )
        payload = await self._request(
            "GET",
            repository,
            absolute_url=f"{_API_ROOT}/search/issues",
            permissions={"issues": "read"},
            params={
                "q": f"repo:{repository} is:issue {query}",
                **_pagination(per_page, page),
            },
        )
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("items"), list
        ):
            raise GitHubAppError("GitHub returned an invalid search response")
        return payload["items"]

    async def list_labels(
        self,
        repository: str,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List labels already defined in a repository."""
        return await self._request(
            "GET",
            repository,
            "/labels",
            permissions={"issues": "read"},
            params=_pagination(per_page, page),
        )

    async def get_file(self, repository: str, path: str) -> Any:
        """Read one repository-relative UTF-8 text file or directory listing."""
        path = _repository_path(path)
        payload = await self._request(
            "GET",
            repository,
            f"/contents/{quote(path, safe='/')}",
            permissions={"contents": "read"},
        )
        if isinstance(payload, Mapping) and payload.get("type") == "file":
            content = payload.get("content")
            if payload.get("encoding") != "base64" or not isinstance(content, str):
                raise GitHubAppError("Repository file is not inline base64 content")
            compact_content = "".join(content.split())
            maximum_encoded_bytes = 4 * ((_MAX_FILE_BYTES + 2) // 3)
            if len(compact_content) > maximum_encoded_bytes:
                raise GitHubAppError(
                    f"Repository file exceeds {_MAX_FILE_BYTES} bytes"
                )
            try:
                decoded = base64.b64decode(compact_content, validate=True)
            except ValueError as error:
                raise GitHubAppError("Repository file has invalid base64 content") from error
            if len(decoded) > _MAX_FILE_BYTES:
                raise GitHubAppError(
                    f"Repository file exceeds {_MAX_FILE_BYTES} bytes"
                )
            try:
                decoded_content = decoded.decode("utf-8")
            except UnicodeDecodeError as error:
                raise GitHubAppError("Repository file is not UTF-8 text") from error
            payload = dict(payload)
            payload.pop("content", None)
            payload["decoded_content"] = decoded_content
        return payload

    async def get_issue_images(
        self,
        repository: str,
        issue_number: int,
    ) -> dict[str, Any]:
        """Load validated GitHub-hosted images from one issue body for the host."""
        repository = self._authorize(repository)
        issue_number = _positive(issue_number, "issue_number")
        credential = await self._token_provider.get_token(
            repository, {"issues": "read"}
        )
        api_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential.token}",
            "User-Agent": "IssueLens-GitHub-MCP/0.1",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=30,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{_API_ROOT}/repos/{repository}/issues/{issue_number}",
                    headers=api_headers,
                )
                response.raise_for_status()
                payload = response.json()
                body = payload.get("body") if isinstance(payload, Mapping) else None
                urls = _extract_issue_image_urls(
                    body if isinstance(body, str) else ""
                )

                images: list[dict[str, Any]] = []
                total_bytes = 0
                for index, url in enumerate(
                    urls[:_MAX_ISSUE_IMAGES], start=1
                ):
                    remaining_bytes = (
                        _MAX_ISSUE_IMAGES_TOTAL_BYTES - total_bytes
                    )
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
        except httpx.HTTPStatusError as error:
            raise GitHubAppError(
                f"GitHub API returned HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise GitHubAppError("GitHub issue image request failed") from error

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
            except httpx.HTTPError as error:
                raise GitHubAppError("GitHub issue image download failed") from error

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise GitHubAppError("GitHub issue image redirect is invalid")
                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()
                media_type = response.headers.get(
                    "content-type", ""
                ).split(";", 1)[0].lower()
                if media_type not in _IMAGE_MEDIA_TYPES:
                    raise GitHubAppError(
                        "GitHub issue attachment is not a supported image"
                    )

                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > maximum_bytes:
                    raise GitHubAppError(
                        "GitHub issue image exceeds the size limit"
                    )

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum_bytes:
                        raise GitHubAppError(
                            "GitHub issue image exceeds the size limit"
                        )
            except (httpx.HTTPError, ValueError) as error:
                raise GitHubAppError("GitHub issue image download failed") from error
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

    async def add_labels(
        self,
        repository: str,
        issue_number: int,
        labels: Sequence[str],
    ) -> Any:
        """Add existing labels without replacing the issue's current labels."""
        return await self._request(
            "POST",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}/labels",
            permissions={"issues": "write"},
            body={"labels": _names(labels, "labels")},
            write=True,
        )

    async def set_assignees(
        self,
        repository: str,
        issue_number: int,
        assignees: Sequence[str],
    ) -> Any:
        """Replace the complete assignee list for one issue."""
        return await self._request(
            "PATCH",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}",
            permissions={"issues": "write"},
            body={"assignees": _names(assignees, "assignees")},
            write=True,
        )

    async def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
    ) -> Any:
        """Post one non-empty issue comment."""
        body = body.strip()
        if not body or len(body) > 65_536:
            raise GitHubAppError(
                "comment body must contain between 1 and 65536 characters"
            )
        return await self._request(
            "POST",
            repository,
            f"/issues/{_positive(issue_number, 'issue_number')}/comments",
            permissions={"issues": "write"},
            body={"body": body},
            write=True,
        )

    async def add_eyes_reaction(
        self,
        repository: str,
        target_kind: ReactionTarget,
        target_id: int,
    ) -> Any:
        """Add the fixed eyes reaction to one supported activity."""
        try:
            path, permission = _REACTION_TARGETS[target_kind]
        except KeyError as error:
            raise GitHubAppError(
                "target_kind must be issue, pull_request, issue_comment, "
                "or pull_request_comment"
            ) from error
        target_id = _positive(target_id, "target_id")
        return await self._request(
            "POST",
            repository,
            path.format(target_id=target_id),
            permissions={permission: "write"},
            body={"content": "eyes"},
            write=True,
        )

    def _authorize(self, repository: str, *, write: bool = False) -> str:
        repository = validate_repository(repository)
        if write and not self._writes_enabled:
            raise GitHubAppError("GitHub write tools are disabled for this server")
        return repository

    async def _request(
        self,
        method: str,
        repository: str,
        path: str = "",
        *,
        absolute_url: str | None = None,
        permissions: Permissions,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        write: bool = False,
    ) -> Any:
        repository = self._authorize(repository, write=write)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "IssueLens-GitHub-MCP/0.1",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        anonymous_fallback = False
        try:
            credential = await self._token_provider.get_token(
                repository,
                permissions,
            )
            headers["Authorization"] = f"Bearer {credential.token}"
        except GitHubAppError:
            if write or method != "GET":
                raise
            anonymous_fallback = True
        url = absolute_url or f"{_API_ROOT}/repos/{repository}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=30,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_HTTP_RESPONSE_BYTES:
                            raise GitHubAppError(
                                "GitHub response is too large; narrow the request"
                            )
                payload = json.loads(content)
        except httpx.HTTPStatusError as error:
            if anonymous_fallback:
                if (
                    error.response.status_code == 403
                    and error.response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    raise GitHubAppError(
                        "GitHub anonymous public-read rate limit exceeded"
                    ) from error
                raise GitHubAppError(
                    f"{repository} is not publicly readable and the "
                    "IssueLens App cannot access it"
                ) from error
            raise GitHubAppError(
                f"GitHub API returned HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise GitHubAppError("GitHub API request failed") from error

        if len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise GitHubAppError("GitHub response is too large; narrow the request")
        return payload


def _pagination(per_page: int, page: int) -> dict[str, int]:
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise GitHubAppError("per_page must be an integer from 1 to 100")
    if type(page) is not int or not 1 <= page <= 100:
        raise GitHubAppError("page must be an integer from 1 to 100")
    return {"per_page": per_page, "page": page}


def _positive(value: int, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubAppError(f"{field} must be a positive integer")
    return value


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise GitHubAppError("since must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise GitHubAppError("since must include a timezone")
    return value


def _repository_path(path: str) -> str:
    if not isinstance(path, str) or path != path.strip() or len(path) > 240:
        raise GitHubAppError("path must be a repository-relative POSIX path")
    parts = pathlib.PurePosixPath(path).parts
    if not path or path.startswith("/") or "//" in path or "\\" in path or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise GitHubAppError("path must be a repository-relative POSIX path")
    return path


def _names(values: Sequence[str], field: str) -> list[str]:
    names = [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]
    if not names or len(names) > 100 or any(len(name) > 100 for name in names):
        raise GitHubAppError(
            f"{field} must contain between 1 and 100 valid names"
        )
    return names


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
