"""Check whether the configured GitHub App is installed for repositories."""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import time
from dataclasses import dataclass

import httpx


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "github_app_mcp" / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.auth import (  # noqa: E402
    GitHubAppError,
    GitHubAppTokenProvider,
    validate_repository,
)
from issuelens_github_mcp.config import (  # noqa: E402
    ConfigurationError,
    GitHubAppConfig,
)


_API_ROOT = "https://api.github.com"
_API_VERSION = "2026-03-10"


@dataclass(frozen=True)
class InstallationCheck:
    repository: str
    status: str
    installation_id: int | None = None
    detail: str | None = None


async def check_installation(
    client: httpx.AsyncClient,
    repository: str,
    app_jwt: str,
) -> InstallationCheck:
    repository = validate_repository(repository)
    response = await client.get(
        f"{_API_ROOT}/repos/{repository}/installation",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": _API_VERSION,
        },
    )
    if response.status_code == 404:
        return InstallationCheck(repository, "not-installed")
    if response.status_code != 200:
        return InstallationCheck(
            repository,
            "error",
            detail=f"GitHub returned HTTP {response.status_code}",
        )

    try:
        installation_id = int(response.json()["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubAppError(
            f"GitHub returned an invalid installation response for {repository}"
        ) from error
    return InstallationCheck(repository, "installed", installation_id)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


async def _run(repositories: list[str]) -> int:
    _load_dotenv()
    config = GitHubAppConfig.from_environment(os.environ)
    provider = GitHubAppTokenProvider(config)
    app_jwt = await provider._app_jwt(time.time())

    checks: list[InstallationCheck] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for repository in repositories:
            checks.append(await check_installation(client, repository, app_jwt))

    for check in checks:
        if check.status == "installed":
            print(
                f"INSTALLED     {check.repository} "
                f"(installation_id={check.installation_id})"
            )
        elif check.status == "not-installed":
            print(f"NOT INSTALLED {check.repository}")
        else:
            print(f"ERROR         {check.repository}: {check.detail}")

    if any(check.status == "error" for check in checks):
        return 2
    if any(check.status == "not-installed" for check in checks):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether the GitHub App configured by GITHUB_APP_ID and "
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI is installed for repositories."
        )
    )
    parser.add_argument(
        "repositories",
        metavar="OWNER/REPOSITORY",
        nargs="+",
        help="one or more repositories to check",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.repositories))
    except (ConfigurationError, GitHubAppError, httpx.HTTPError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
