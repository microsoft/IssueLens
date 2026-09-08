import asyncio
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
    async def test_pull_request_write_token_is_repository_restricted(self, _):
        credential = await self.provider.get_token(
            "microsoft/IssueLens", {"pull_requests": "write"}
        )

        token_call = self.calls[-1]
        self.assertEqual(
            json.loads(token_call.content),
            {
                "repositories": ["IssueLens"],
                "permissions": {"pull_requests": "write"},
            },
        )
        self.assertEqual(credential.repository, "microsoft/IssueLens")
        self.assertEqual(
            credential.permissions,
            (("pull_requests", "write"),),
        )

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

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_repository_and_permissions_are_validated_before_network(self, _):
        with self.assertRaisesRegex(GitHubAppError, "owner/repository"):
            await self.provider.get_token("IssueLens", {"issues": "read"})
        credential = await self.provider.get_token(
            "microsoft/IssueLens", {"contents": "write"}
        )
        self.assertEqual(credential.permissions, (("contents", "write"),))

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


class BotIdentityTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, handler, loader=None):
        async def load_key():
            return "test-key"

        return GitHubAppTokenProvider(
            GitHubAppConfig(
                app_id="1816975",
                private_key_secret_uri=(
                    "https://issuelens.vault.azure.net/secrets/github-app-key"
                ),
            ),
            private_key_loader=loader or load_key,
            transport=httpx.MockTransport(handler),
        )

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_identity_is_verified_and_cached_for_concurrent_writes(self, encode):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.host, "api.github.com")
            if request.url.path == "/app":
                self.assertEqual(request.headers["Authorization"], "Bearer app-jwt")
                return httpx.Response(200, json={"id": 1816975, "slug": "issuelens"})
            self.assertEqual(request.url.path, "/users/issuelens[bot]")
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, json={
                "id": 7654321, "login": "issuelens[bot]", "type": "Bot",
            })

        provider = self.provider(handler)
        self.assertEqual(calls, [])
        identities = await asyncio.gather(
            provider.get_bot_identity(), provider.get_bot_identity()
        )
        self.assertEqual(identities, [(
            "issuelens[bot]", "7654321+issuelens[bot]@users.noreply.github.com",
        )] * 2)
        self.assertEqual(len(calls), 2)
        encode.assert_called_once()

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_invalid_app_cannot_choose_a_user_endpoint(self, _):
        for payload in (
            [], {}, {"id": True, "slug": "issuelens"},
            {"id": 111, "slug": "issuelens"},
            {"id": 1816975, "slug": ""},
            {"id": 1816975, "slug": "../other?token=secret"},
            {"id": 1816975, "slug": "a" * 101},
        ):
            with self.subTest(payload=payload):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(200, json=payload)

                with self.assertRaisesRegex(GitHubAppError, "verify.*bot identity"):
                    await self.provider(handler).get_bot_identity()
                self.assertEqual(len(calls), 1)

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_bot_requires_exact_login_type_and_positive_user_id(self, _):
        valid = {"id": 7654321, "login": "issuelens[bot]", "type": "Bot"}
        for payload in (
            [], {}, {**valid, "login": "other[bot]"},
            {**valid, "type": "User"}, {**valid, "id": True},
            {**valid, "id": 0}, {**valid, "id": "7654321"},
        ):
            with self.subTest(payload=payload):
                def handler(request):
                    if request.url.path == "/app":
                        return httpx.Response(200, json={
                            "id": 1816975, "slug": "issuelens",
                        })
                    return httpx.Response(200, json=payload)

                with self.assertRaisesRegex(GitHubAppError, "verify.*bot identity"):
                    await self.provider(handler).get_bot_identity()

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="app-jwt")
    async def test_api_errors_are_sanitized_and_failures_not_cached(self, _):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="private-key app-jwt installation-token")

        provider = self.provider(handler)
        for attempt in range(2):
            with self.assertRaises(GitHubAppError) as caught:
                await provider.get_bot_identity()
            self.assertEqual(str(caught.exception), "Could not verify the GitHub App bot identity")
            self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(len(calls), 2)

    async def test_key_loader_errors_are_sanitized(self):
        async def loader():
            raise RuntimeError("private-key-secret")

        def handler(request):
            self.fail("Key loading failure must not make HTTP requests")

        with self.assertRaisesRegex(GitHubAppError, "verify.*bot identity") as caught:
            await self.provider(handler, loader).get_bot_identity()
        self.assertNotIn("private-key-secret", str(caught.exception))


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
