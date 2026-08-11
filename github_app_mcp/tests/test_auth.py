import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

import httpx


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.auth import (  # noqa: E402
    GitHubAppError,
    GitHubAppTokenProvider,
)
from issuelens_github_mcp.config import (  # noqa: E402
    ConfigurationError,
    GitHubAppConfig,
    parse_key_vault_secret_uri,
)


class GitHubAppTokenProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if request.url.path in {
                "/repos/microsoft/IssueLens/installation",
                "/repos/microsoft/other/installation",
            }:
                return httpx.Response(200, json={"id": 1234})
            if request.url.path == "/app/installations/1234/access_tokens":
                body = json.loads(request.content)
                repository = body["repositories"][0]
                permission = next(iter(body["permissions"].values()))
                return httpx.Response(
                    201,
                    json={
                        "token": f"token-{repository}-{permission}",
                        "expires_at": "2030-01-01T01:00:00Z",
                    },
                )
            return httpx.Response(404)

        self.provider = GitHubAppTokenProvider(
            GitHubAppConfig(
                app_id="1816975",
                private_key_secret_uri=(
                    "https://issuelens.vault.azure.net/secrets/github-app-key"
                ),
            ),
            private_key_loader=self._load_key,
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_700_000_000,
        )

    async def _load_key(self) -> str:
        return "test-key"

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_token_request_is_repository_and_permission_restricted(self, _):
        credential = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )

        token_call = self.calls[-1]
        self.assertEqual(
            json.loads(token_call.content),
            {
                "repositories": ["IssueLens"],
                "permissions": {"issues": "read"},
            },
        )
        self.assertEqual(credential.repository, "microsoft/IssueLens")
        self.assertEqual(credential.permissions, (("issues", "read"),))

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_token_is_reused_within_one_provider_session(self, _):
        first = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )
        second = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )

        self.assertIs(first, second)
        token_calls = [
            call for call in self.calls if call.url.path.endswith("/access_tokens")
        ]
        self.assertEqual(len(token_calls), 1)

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_provider_sessions_do_not_share_cached_tokens(self, _):
        second_provider = GitHubAppTokenProvider(
            self.provider._config,
            private_key_loader=self._load_key,
            transport=self.provider._transport,
            clock=lambda: 1_700_000_000,
        )

        await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )
        await second_provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )

        token_calls = [
            call for call in self.calls if call.url.path.endswith("/access_tokens")
        ]
        self.assertEqual(len(token_calls), 2)

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_same_installation_does_not_share_tokens_between_repositories(self, _):
        first = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )
        second = await self.provider.get_token(
            "microsoft/other", {"issues": "read"}
        )

        self.assertNotEqual(first.token, second.token)
        token_calls = [
            call
            for call in self.calls
            if call.url.path.endswith("/access_tokens")
        ]
        self.assertEqual(len(token_calls), 2)

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_different_permissions_do_not_share_tokens(self, _):
        read = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )
        write = await self.provider.get_token(
            "microsoft/IssueLens", {"issues": "write"}
        )

        self.assertNotEqual(read.token, write.token)

    async def test_repository_and_permissions_are_validated_before_network(self):
        with self.assertRaisesRegex(GitHubAppError, "owner/repository"):
            await self.provider.get_token("IssueLens", {"issues": "read"})
        with self.assertRaisesRegex(GitHubAppError, "Unsupported"):
            await self.provider.get_token(
                "microsoft/IssueLens", {"contents": "write"}
            )
        self.assertEqual(self.calls, [])

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_stale_installation_is_rediscovered_once(self, _):
        discoveries = 0

        def handler(request):
            nonlocal discoveries
            if request.url.path.endswith("/installation"):
                discoveries += 1
                return httpx.Response(200, json={
                    "id": 111 if discoveries == 1 else 222
                })
            if request.url.path == "/app/installations/111/access_tokens":
                return httpx.Response(404)
            if request.url.path == "/app/installations/222/access_tokens":
                return httpx.Response(201, json={
                    "token": "replacement-token",
                    "expires_at": "2030-01-01T01:00:00Z",
                })
            return httpx.Response(500)

        provider = GitHubAppTokenProvider(
            self.provider._config,
            private_key_loader=self._load_key,
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_700_000_000,
        )

        credential = await provider.get_token(
            "microsoft/IssueLens", {"issues": "read"}
        )

        self.assertEqual(discoveries, 2)
        self.assertEqual(credential.installation_id, 222)
        self.assertEqual(credential.token, "replacement-token")

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_malformed_token_response_is_safely_rejected(self, _):
        def handler(request):
            if request.url.path.endswith("/installation"):
                return httpx.Response(200, json={"id": 1234})
            return httpx.Response(201, json={
                "token": 123,
                "expires_at": "already-expired",
            })

        provider = GitHubAppTokenProvider(
            self.provider._config,
            private_key_loader=self._load_key,
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_700_000_000,
        )

        with self.assertRaisesRegex(GitHubAppError, "invalid installation"):
            await provider.get_token(
                "microsoft/IssueLens", {"issues": "read"}
            )


class ConfigurationTests(unittest.TestCase):
    def test_environment_requires_numeric_app_id_and_key_vault_uri(self):
        config = GitHubAppConfig.from_environment({
            "GITHUB_APP_ID": "1816975",
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI": (
                "https://issuelens.vault.azure.net/secrets/github-app-key/123"
            ),
        })

        self.assertEqual(config.app_id, "1816975")
        secret = parse_key_vault_secret_uri(config.private_key_secret_uri)
        self.assertEqual(secret.secret_name, "github-app-key")
        self.assertEqual(secret.secret_version, "123")

    def test_non_key_vault_secret_uri_is_rejected(self):
        for secret_uri in (
            "https://example.com/secrets/github-app-key",
            "https://a.vault.azure.net/secrets/github-app-key",
            "https://issuelens.vault.azure.net/secrets/key%2Fother",
            "https://issuelens.vault.azure.net/secrets/key_name",
        ):
            with self.subTest(secret_uri=secret_uri):
                with self.assertRaises(ConfigurationError):
                    GitHubAppConfig.from_environment({
                        "GITHUB_APP_ID": "1816975",
                        "GITHUB_APP_PRIVATE_KEY_SECRET_URI": secret_uri,
                    })


if __name__ == "__main__":
    unittest.main()
