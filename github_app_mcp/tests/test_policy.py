import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from issuelens_github_mcp import policy


SOURCE = "microsoft/IssueLens"
DESTINATION = "microsoft/team-knowledge"
CONFIG_PATH = ".github/issuelens.yml"
INSTRUCTION_PATH = ".github/issuelens/team-memory.md"
PATH_CONFIG = (
    "version: 1\ninstructions:\n  team_memory:\n"
    f"    path: {INSTRUCTION_PATH}\n"
)
DESTINATION_CONFIG = PATH_CONFIG + f"    wiki_repository: {DESTINATION}\n"


class NotFoundError(RuntimeError):
    status_code = 404


class FakeClient:
    def __init__(self, files):
        self.files = files
        self.calls = []

    async def get_file(self, repository, path):
        self.calls.append((repository, path))
        if path == ".github":
            entries = [
                {"name": file_path.rsplit("/", 1)[-1], "type": "file"}
                for file_path in self.files
                if file_path.rpartition("/")[0] == ".github"
            ]
            if not entries:
                raise NotFoundError("Not Found")
            return entries
        if path not in self.files:
            raise NotFoundError("Not Found")
        payload = self.files[path]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, str):
            return {"type": "file", "decoded_content": payload}
        return payload


class PolicyParserTests(unittest.TestCase):
    def test_public_parser_keeps_domain_to_path_api(self):
        content = DESTINATION_CONFIG + "  labeling:\n    path: docs/labels.md\n"
        with patch.object(policy, "_parse_config", wraps=policy._parse_config) as parser:
            self.assertEqual(policy.parse_config(content), {
                "team_memory": INSTRUCTION_PATH,
                "labeling": "docs/labels.md",
            })
        parser.assert_called_once_with(content)

    def test_valid_github_repository_identifiers_are_preserved(self):
        values = (
            SOURCE, DESTINATION, "my-team/.github", "owner/a_b.c-d",
            "owner/.hidden", "owner/repo.wiki", "owner/git", "owner/repo..name",
            "owner/-repo", "owner/repo.", "a/b", "owner/.gitignore",
            f"{'a' * 39}/{'b' * 100}",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(policy.validate_wiki_repository(value), value)
                self.assertEqual(
                    policy.parse_config(PATH_CONFIG + f"    wiki_repository: {value}\n"),
                    {"team_memory": INSTRUCTION_PATH},
                )

    def test_unsafe_or_non_repository_destinations_are_rejected(self):
        values = (
            None, True, 1, [], {}, "", " owner/repo", "owner/repo ",
            "owner/repo\n", "owner/re\tpo", "owner/re\x00po", "owner/re po",
            ".", "..", "owner/.", "owner/..", "./repo", "../repo",
            "owner/repo.git", "owner/repo.wiki.git", "owner/repo.WIKI.GIT",
            "https://github.com/owner/repo", "https://token@github.com/owner/repo",
            "git@github.com:owner/repo", "ssh://git@github.com/owner/repo",
            "file:///owner/repo", "C:/repo", "owner\\repo", "/owner/repo",
            "owner/repo/path", "owner//repo", "owner/repo/", "owner/repo#main",
            "owner/repo?ref=main", "owner/repo@main", "owner/repo:main",
            "owner/repo%2Fother", "owner/repo~1", "owner/repo^", "-owner/repo",
            "owner-/repo", "my--team/repo", "my_team/repo", "my.team/repo",
            "owner/r\u00e9po", f"{'a' * 40}/repo", f"owner/{'b' * 101}",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.validate_wiki_repository(value)
                content = yaml.safe_dump({
                    "version": 1,
                    "instructions": {"team_memory": {
                        "path": INSTRUCTION_PATH,
                        "wiki_repository": value,
                    }},
                })
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.parse_config(content)

    def test_malformed_and_unknown_config_fields_fail_closed(self):
        invalid = (
            "", "[]", "null", "version: true", "version: '1'", "version: 1.0",
            "version: 2", "instructions: {}", "version: 1\nunknown: true",
            "version: 1\ninstructions: []", "version: 1\ninstructions: null",
            "version: 1\ninstructions: string",
            "version: 1\ninstructions:\n  unknown:\n    path: docs/policy.md",
            "version: 1\ninstructions:\n  team_memory: null",
            "version: 1\ninstructions:\n  team_memory: []",
            "version: 1\ninstructions:\n  team_memory: true",
            "version: 1\ninstructions:\n  team_memory: {}",
            "version: 1\ninstructions:\n  team_memory:\n    wiki_repository: owner/repo",
            "version: 1\ninstructions:\n  team_memory:\n    path: []",
            "version: 1\ninstructions:\n  team_memory:\n    path: null",
            "version: 1\ninstructions:\n  team_memory:\n    path: 42",
            PATH_CONFIG + "    unknown: true\n",
            "version: 1\n1: value", "version: 1\n? [bad, key]\n: value",
            "version: 1\ninstructions:\n  true: {}",
            "version: 1\ninstructions:\n  team_memory:\n    false: value",
            "!!map scalar", "version: [", "!!python/object:builtins.object {}",
        )
        for content in invalid:
            with self.subTest(content=content):
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.parse_config(content)

    def test_other_domains_only_accept_path(self):
        for domain in policy.INSTRUCTION_DOMAINS - {"team_memory"}:
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(policy.IssueLensConfigError, "Unknown"):
                    policy.parse_config(
                        f"version: 1\ninstructions:\n  {domain}:\n"
                        "    path: docs/policy.md\n    wiki_repository: owner/repo\n"
                    )

    def test_duplicate_yaml_keys_at_every_level_are_rejected(self):
        invalid = (
            "version: 1\nversion: 1\n",
            "version: 1\ninstructions: {}\ninstructions: {}\n",
            PATH_CONFIG + "  team_memory:\n    path: docs/other.md\n",
            PATH_CONFIG + "    path: docs/other.md\n",
            DESTINATION_CONFIG + "    wiki_repository: other/knowledge\n",
            DESTINATION_CONFIG + f"    'wiki_repository': {DESTINATION}\n",
        )
        for content in invalid:
            with self.subTest(content=content):
                with self.assertRaisesRegex(policy.IssueLensConfigError, "Duplicate YAML key"):
                    policy.parse_config(content)

    def test_loader_does_not_modify_global_yaml_behavior(self):
        self.assertEqual(yaml.safe_load("version: 1\nversion: 2"), {"version": 2})

    def test_anchors_aliases_and_multiple_documents_remain_forbidden(self):
        invalid = (
            "version: &version 1", "version: *version",
            "version: 1\n---\nversion: 1",
            "version: 1\n---\n",
            PATH_CONFIG + "    wiki_repository: &target owner/repo",
        )
        for content in invalid:
            with self.subTest(content=content):
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.parse_config(content)

    def test_config_size_and_input_types_are_bounded(self):
        prefix = "version: 1\n#"
        self.assertEqual(policy.parse_config(
            prefix + "x" * (policy.MAX_CONFIG_BYTES - len(prefix))
        ), {})
        for content in (
            prefix + "x" * policy.MAX_CONFIG_BYTES,
            prefix + "\u00e9" * (policy.MAX_CONFIG_BYTES // 2),
            None, b"version: 1", [], 1,
        ):
            with self.subTest(kind=type(content).__name__):
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.parse_config(content)

    def test_instruction_paths_remain_bounded_and_repository_relative(self):
        invalid = (
            "", " docs/policy.md", "docs/policy.md ", "../policy.md",
            "docs/../policy.md", "./policy.md", "/docs/policy.md", "docs//policy.md",
            "docs\\policy.md", "https://example.com/policy.md", "C:/policy.md",
            "docs/policy.md?ref=main", "docs/policy.md#ref", "policy.txt",
            "//[invalid/policy.md", "docs/pol\x00icy.md", "docs/pol\nicy.md",
            "x" * 238 + ".md",
        )
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(policy.IssueLensConfigError):
                    policy.parse_config(yaml.safe_dump({
                        "version": 1,
                        "instructions": {"team_memory": {"path": path}},
                    }))
        for path in ("docs/my policy.MD", "x" * 237 + ".md"):
            with self.subTest(path=path):
                self.assertEqual(policy.parse_config(yaml.safe_dump({
                    "version": 1,
                    "instructions": {"team_memory": {"path": path}},
                })), {"team_memory": path})


class PolicyLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_destination_is_a_validated_field_not_markdown(self):
        content = "Topics and access. wiki_repository: unrelated/from-markdown"
        client = FakeClient({CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: content})

        result = await policy.load_instruction(client, SOURCE, "team_memory")

        self.assertEqual(result, {
            "repository": SOURCE,
            "domain": "team_memory",
            "configStatus": "loaded",
            "configPath": CONFIG_PATH,
            "source": "configured",
            "path": INSTRUCTION_PATH,
            "content": content,
            "wiki_repository": DESTINATION,
        })
        self.assertEqual(client.calls, [
            (SOURCE, ".github"), (SOURCE, CONFIG_PATH), (SOURCE, INSTRUCTION_PATH),
        ])

    async def test_resolver_uses_same_loader_and_parses_config_once(self):
        client = FakeClient({CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: "Topics"})
        with (
            patch.object(policy, "load_instruction", wraps=policy.load_instruction) as loader,
            patch.object(policy, "_parse_config", wraps=policy._parse_config) as parser,
        ):
            self.assertEqual(await policy.resolve_wiki_repository(client, SOURCE), DESTINATION)
        loader.assert_awaited_once_with(client, SOURCE, "team_memory")
        parser.assert_called_once_with(DESTINATION_CONFIG)
        self.assertEqual(client.calls, [
            (SOURCE, ".github"), (SOURCE, CONFIG_PATH), (SOURCE, INSTRUCTION_PATH),
        ])

    async def test_absent_config_defaults_to_source_repository(self):
        client = FakeClient({})
        result = await policy.load_instruction(client, SOURCE, "team_memory")
        self.assertEqual(result, {
            "repository": SOURCE, "domain": "team_memory", "configStatus": "absent",
            "configPath": None, "source": "built-in", "path": None, "content": None,
            "wiki_repository": SOURCE,
        })
        self.assertEqual(client.calls, [(SOURCE, ".github")])
        self.assertEqual(await policy.resolve_wiki_repository(FakeClient({}), SOURCE), SOURCE)

    async def test_omitted_domain_defaults_without_reading_other_instructions(self):
        client = FakeClient({CONFIG_PATH: (
            "version: 1\ninstructions:\n  planning:\n    path: docs/planning.md\n"
        )})
        result = await policy.load_instruction(client, SOURCE, "team_memory")
        self.assertEqual(result["wiki_repository"], SOURCE)
        self.assertEqual(result["configStatus"], "loaded")
        self.assertEqual(result["source"], "built-in")
        self.assertIsNone(result["content"])
        self.assertEqual(client.calls, [(SOURCE, ".github"), (SOURCE, CONFIG_PATH)])

    async def test_omitted_destination_defaults_even_when_markdown_names_a_target(self):
        content = "wiki_repository: unrelated/from-markdown"
        client = FakeClient({CONFIG_PATH: PATH_CONFIG, INSTRUCTION_PATH: content})
        result = await policy.load_instruction(client, SOURCE, "team_memory")
        self.assertEqual(result["wiki_repository"], SOURCE)
        self.assertEqual(result["content"], content)
        self.assertEqual(result["source"], "configured")
        self.assertEqual(await policy.resolve_wiki_repository(client, SOURCE), SOURCE)

    async def test_multiple_sources_can_share_a_destination(self):
        for source in (SOURCE, "another-team/another-repo"):
            with self.subTest(source=source):
                client = FakeClient({CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: "Topics"})
                result = await policy.load_instruction(client, source, "team_memory")
                self.assertEqual(result["repository"], source)
                self.assertEqual(result["wiki_repository"], DESTINATION)
                self.assertEqual({repository for repository, _ in client.calls}, {source})

    async def test_invalid_source_is_rejected_before_any_network_read(self):
        for source in (None, [], "owner/..", "owner/.", "owner/repo/extra", "https://github.com/owner/repo"):
            with self.subTest(source=source):
                client = FakeClient({})
                for domain in ("team_memory", "labeling"):
                    with self.assertRaises(policy.IssueLensConfigError):
                        await policy.load_instruction(client, source, domain)
                with self.assertRaises(policy.IssueLensConfigError):
                    await policy.resolve_wiki_repository(client, source)
                self.assertEqual(client.calls, [])

    async def test_source_uses_normal_repository_validation_for_all_domains(self):
        for source in ("owner/project.git", "owner/project.wiki.git", "my--team/project"):
            for domain in policy.INSTRUCTION_DOMAINS:
                with self.subTest(source=source, domain=domain):
                    client = FakeClient({})
                    result = await policy.load_instruction(client, f" {source} ", domain)
                    self.assertEqual(result["repository"], source)
                    self.assertEqual(result["source"], "built-in")
                    self.assertTrue(all(repository == source for repository, _ in client.calls))
                    if domain == "team_memory":
                        self.assertEqual(result["wiki_repository"], source)

    async def test_configured_and_legacy_instructions_load_for_dot_git_source(self):
        source = "owner/project.git"
        for domain in ("labeling", "planning", "team_memory"):
            with self.subTest(domain=domain):
                config = f"version: 1\ninstructions:\n  {domain}:\n    path: {INSTRUCTION_PATH}\n"
                if domain == "team_memory":
                    config += f"    wiki_repository: {DESTINATION}\n"
                client = FakeClient({CONFIG_PATH: config, INSTRUCTION_PATH: "Project policy"})
                result = await policy.load_instruction(client, source, domain)
                self.assertEqual(result["content"], "Project policy")
                self.assertEqual(result["source"], "configured")
                self.assertEqual(client.calls, [
                    (source, ".github"), (source, CONFIG_PATH), (source, INSTRUCTION_PATH),
                ])
                if domain == "team_memory":
                    self.assertEqual(result["wiki_repository"], DESTINATION)
        result = await policy.load_instruction(
            FakeClient({".github/label-instructions.md": "Legacy labels"}), source, "labeling"
        )
        self.assertEqual(result["source"], "legacy")
        self.assertEqual(result["content"], "Legacy labels")

    async def test_dot_git_source_does_not_relax_explicit_destination_rules(self):
        client = FakeClient({
            CONFIG_PATH: PATH_CONFIG + "    wiki_repository: owner/destination.git\n",
            INSTRUCTION_PATH: "Must not be read",
        })
        with self.assertRaisesRegex(policy.IssueLensConfigError, "wiki_repository"):
            await policy.load_instruction(client, "owner/project.git", "team_memory")
        self.assertEqual(client.calls, [
            ("owner/project.git", ".github"), ("owner/project.git", CONFIG_PATH),
        ])

    async def test_invalid_policy_stops_before_markdown_and_never_defaults(self):
        for config in (
            "version: 2",
            PATH_CONFIG + "    wiki_repository: https://github.com/owner/repo",
            DESTINATION_CONFIG + "    wiki_repository: other/repo",
            "version: 1\ninstructions:\n  team_memory:\n    wiki_repository: owner/repo",
            PATH_CONFIG + "    unknown: true",
        ):
            with self.subTest(config=config):
                client = FakeClient({CONFIG_PATH: config, INSTRUCTION_PATH: "Topics"})
                with self.assertRaises(policy.IssueLensConfigError):
                    await policy.resolve_wiki_repository(client, SOURCE)
                self.assertEqual(client.calls, [(SOURCE, ".github"), (SOURCE, CONFIG_PATH)])

    async def test_missing_configured_markdown_blocks_both_loading_and_resolution(self):
        for config in (PATH_CONFIG, DESTINATION_CONFIG):
            with self.subTest(config=config):
                client = FakeClient({CONFIG_PATH: config})
                with self.assertRaisesRegex(policy.IssueLensConfigError, "file not found"):
                    await policy.load_instruction(client, SOURCE, "team_memory")
                with self.assertRaisesRegex(policy.IssueLensConfigError, "file not found"):
                    await policy.resolve_wiki_repository(client, SOURCE)
                self.assertEqual(client.calls[-1], (SOURCE, INSTRUCTION_PATH))

    async def test_resolution_validates_markdown_payload_and_content_bounds(self):
        for payload in (
            [], {}, {"decoded_content": None}, {"decoded_content": b"not text"},
            "x" * (policy.MAX_INSTRUCTION_BYTES + 1),
            "\u00e9" * (policy.MAX_INSTRUCTION_BYTES // 2 + 1),
            RuntimeError("HTTP 403"),
        ):
            with self.subTest(kind=type(payload).__name__):
                client = FakeClient({CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: payload})
                with self.assertRaises(policy.IssueLensConfigError):
                    await policy.resolve_wiki_repository(client, SOURCE)
                self.assertEqual(client.calls[-1], (SOURCE, INSTRUCTION_PATH))
        client = FakeClient({
            CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: "x" * policy.MAX_INSTRUCTION_BYTES,
        })
        self.assertEqual(await policy.resolve_wiki_repository(client, SOURCE), DESTINATION)

    async def test_other_domains_keep_result_shape_and_isolated_content(self):
        client = FakeClient({
            CONFIG_PATH: DESTINATION_CONFIG + "  labeling:\n    path: docs/labels.md\n",
            INSTRUCTION_PATH: "Private memory instructions",
            "docs/labels.md": "Label instructions",
        })
        result = await policy.load_instruction(client, SOURCE, "labeling")
        self.assertEqual(result, {
            "repository": SOURCE, "domain": "labeling", "configStatus": "loaded",
            "configPath": CONFIG_PATH, "source": "configured", "path": "docs/labels.md",
            "content": "Label instructions",
        })
        self.assertNotIn((SOURCE, INSTRUCTION_PATH), client.calls)
        for domain in policy.INSTRUCTION_DOMAINS - {"team_memory"}:
            with self.subTest(domain=domain):
                result = await policy.load_instruction(FakeClient({}), SOURCE, domain)
                self.assertNotIn("wiki_repository", result)
        result = await policy.load_instruction(FakeClient({
            ".github/label-instructions.md": "Legacy labels",
        }), SOURCE, "labeling")
        self.assertEqual(result["source"], "legacy")
        self.assertNotIn("wiki_repository", result)

    async def test_destination_does_not_depend_on_environment(self):
        for environment in ({}, {
            "ISSUELENS_WIKI_WRITE_REPOSITORIES": "unrelated/allowlist",
            "GITHUB_REPOSITORY": "unrelated/source",
        }):
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    client = FakeClient({
                        CONFIG_PATH: DESTINATION_CONFIG, INSTRUCTION_PATH: "Topics",
                    })
                    self.assertEqual(
                        await policy.resolve_wiki_repository(client, SOURCE), DESTINATION,
                    )
                    self.assertEqual(
                        await policy.resolve_wiki_repository(FakeClient({}), SOURCE), SOURCE,
                    )


class StandalonePolicyTests(unittest.TestCase):
    def test_package_import_and_resolution_work_without_root_modules(self):
        package_source = pathlib.Path(__file__).resolve().parents[1] / "src"
        code = (
            "import asyncio, pathlib, sys\n"
            f"sys.path.insert(0, {str(package_source)!r})\n"
            "class RejectRootImports:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname.split('.')[0] in {'issuelens_config', 'github_app_mcp'}:\n"
            "            raise AssertionError('Root import: ' + fullname)\n"
            "sys.meta_path.insert(0, RejectRootImports())\n"
            "from issuelens_github_mcp import policy\n"
            f"assert pathlib.Path(policy.__file__).resolve().parent.parent == pathlib.Path({str(package_source)!r})\n"
            "assert policy.parse_config('version: 1') == {}\n"
            "class FakeClient:\n"
            "    async def get_file(self, repository, path):\n"
            "        return []\n"
            "assert asyncio.run(policy.resolve_wiki_repository(FakeClient(), 'owner/repo')) == 'owner/repo'\n"
            "print('Standalone policy: PASS')\n"
        )
        with tempfile.TemporaryDirectory() as outside_root:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code], cwd=outside_root,
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Standalone policy: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
