import os
import pathlib
import sys
import unittest

import httpx


SCRIPTS_ROOT = pathlib.Path(__file__).parents[1] / "scripts"
sys.path.insert(0, os.fspath(SCRIPTS_ROOT))

from check_installations import check_installation  # noqa: E402
from issuelens_github_mcp.auth import GitHubAppError  # noqa: E402


class CheckInstallationTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, status_code, payload=None):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.path,
                "/repos/microsoft/IssueLens/installation",
            )
            self.assertEqual(request.headers["Authorization"], "Bearer app-jwt")
            return httpx.Response(status_code, json=payload)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await check_installation(
                client,
                "microsoft/IssueLens",
                "app-jwt",
            )

    async def test_installed_repository_returns_installation_id(self):
        check = await self._check(200, {"id": 1234})

        self.assertEqual(check.status, "installed")
        self.assertEqual(check.installation_id, 1234)

    async def test_missing_installation_is_reported_separately(self):
        check = await self._check(404, {"message": "Not Found"})

        self.assertEqual(check.status, "not-installed")
        self.assertIsNone(check.installation_id)

    async def test_authentication_failure_is_not_reported_as_not_installed(self):
        check = await self._check(401, {"message": "Bad credentials"})

        self.assertEqual(check.status, "error")
        self.assertEqual(check.detail, "GitHub returned HTTP 401")

    async def test_invalid_success_response_is_rejected(self):
        with self.assertRaisesRegex(GitHubAppError, "invalid installation"):
            await self._check(200, {"account": {}})


if __name__ == "__main__":
    unittest.main()
