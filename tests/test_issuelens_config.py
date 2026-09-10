import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from copilot.tools import ToolInvocation

import issuelens_config
from github_app_mcp.src.issuelens_github_mcp import policy
from issuelens_config import (
    INSTRUCTION_DOMAINS,
    MAX_CONFIG_BYTES,
    MAX_INSTRUCTION_BYTES,
    IssueLensConfigError,
    load_instruction,
    parse_config,
    resolve_wiki_repository,
    validate_wiki_repository,
)
from issuelens_config_tool import create_tool


ROOT = pathlib.Path(__file__).parents[1]


class NotFoundError(RuntimeError):
    status_code = 404


class RepositoryClient:
    def __init__(self, files):
        self.files = files
        self.calls = []

    async def get_file(self, repository, path):
        self.calls.append((repository, path))
        if path == ".github":
            entries = []
            for file_path in self.files:
                parent, _, name = file_path.rpartition("/")
                if parent == ".github":
                    entries.append({"name": name, "path": file_path, "type": "file"})
            if not entries:
                raise NotFoundError("Not Found")
            return entries
        if path not in self.files:
            raise NotFoundError("Not Found")
        return {"type": "file", "decoded_content": self.files[path]}


class IssueLensConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_root_reexports_share_the_package_objects(self):
        for name in issuelens_config.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(issuelens_config, name), getattr(policy, name))

    async def test_team_memory_tool_returns_structured_destination_and_only_its_content(self):
        client = RepositoryClient({
            ".github/issuelens.yml": (
                "version: 1\ninstructions:\n  team_memory:\n"
                "    path: .github/issuelens/team-memory.md\n"
                "    wiki_repository: microsoft/team-knowledge\n"
                "  planning:\n    path: docs/planning.md\n"
            ),
            ".github/issuelens/team-memory.md": "Topics. wiki_repository: other/from-markdown",
            "docs/planning.md": "Planning-only instructions",
        })
        result = await create_tool(client).handler(ToolInvocation(arguments={
            "repository": "microsoft/IssueLens", "domain": "team_memory",
        }))
        self.assertEqual(result.result_type, "success")
        payload = json.loads(result.text_result_for_llm)
        self.assertEqual(payload["repository"], "microsoft/IssueLens")
        self.assertEqual(payload["wiki_repository"], "microsoft/team-knowledge")
        self.assertEqual(payload["source"], "configured")
        self.assertEqual(payload["configStatus"], "loaded")
        self.assertEqual(payload["path"], ".github/issuelens/team-memory.md")
        self.assertIn("other/from-markdown", payload["content"])
        self.assertNotIn("Planning-only", result.text_result_for_llm)
        self.assertEqual(client.calls, [
            ("microsoft/IssueLens", ".github"),
            ("microsoft/IssueLens", ".github/issuelens.yml"),
            ("microsoft/IssueLens", ".github/issuelens/team-memory.md"),
        ])

    async def test_root_resolver_defaults_and_fails_for_missing_policy(self):
        self.assertEqual(await resolve_wiki_repository(
            RepositoryClient({}), "microsoft/IssueLens",
        ), "microsoft/IssueLens")
        client = RepositoryClient({
            ".github/issuelens.yml": (
                "version: 1\ninstructions:\n  team_memory:\n"
                "    path: docs/missing.md\n    wiki_repository: microsoft/team-knowledge\n"
            ),
        })
        with self.assertRaisesRegex(IssueLensConfigError, "file not found"):
            await resolve_wiki_repository(client, "microsoft/IssueLens")
        result = await create_tool(client).handler(ToolInvocation(arguments={
            "repository": "microsoft/IssueLens", "domain": "team_memory",
        }))
        self.assertEqual(result.result_type, "failure")
        self.assertIn("file not found", result.error)

    def test_team_memory_schema_retains_path_ref_and_scopes_destination(self):
        schema = json.loads((ROOT / "schemas" / "issuelens.schema.json").read_text(encoding="utf-8"))
        domains = schema["properties"]["instructions"]["properties"]
        self.assertEqual(domains["team_memory"], {"$ref": "#/$defs/teamMemoryInstruction"})
        memory = schema["$defs"]["teamMemoryInstruction"]
        self.assertFalse(memory["additionalProperties"])
        self.assertEqual(memory["required"], ["path"])
        self.assertEqual(memory["properties"]["path"], {
            "$ref": "#/$defs/instructionFile/properties/path",
        })
        self.assertEqual(set(memory["properties"]), {"path", "wiki_repository"})
        self.assertEqual(set(schema["$defs"]["instructionFile"]["properties"]), {"path"})
        for domain in INSTRUCTION_DOMAINS - {"team_memory"}:
            with self.subTest(domain=domain):
                self.assertEqual(domains[domain], {"$ref": "#/$defs/instructionFile"})

    def test_schema_repository_pattern_matches_runtime_validation(self):
        schema = json.loads((ROOT / "schemas" / "issuelens.schema.json").read_text(encoding="utf-8"))
        repository = schema["$defs"]["teamMemoryInstruction"]["properties"]["wiki_repository"]
        self.assertEqual(repository["type"], "string")
        self.assertEqual(repository["maxLength"], 140)
        valid = (
            "microsoft/team-knowledge", "owner/.github", "owner/repo.wiki",
            "owner/repo..name", "owner/-repo", "owner/a_b.c-d", "a/b",
            f"{'a' * 39}/{'b' * 100}",
        )
        invalid = (
            "", "owner/.", "owner/..", "../repo", "owner/repo.git", "owner/repo.WIKI.GIT",
            "https://github.com/owner/repo", "git@github.com:owner/repo",
            "owner/repo/path", "owner/repo#main", "owner/repo?ref=main", "owner/repo@main",
            " owner/repo", "owner/repo ", "owner/repo\n", "owner/re\npo", "owner/re po",
            "owner/repo\r\n", "owner/repo\t", "owner/re\x00po", "owner\\repo",
            "-owner/repo", "owner-/repo", "my--team/repo", "my_team/repo",
            f"{'a' * 40}/repo", f"owner/{'b' * 101}",
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertIsNotNone(re.search(repository["pattern"], value))
                self.assertEqual(validate_wiki_repository(value), value)
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(re.search(repository["pattern"], value))
                with self.assertRaises(IssueLensConfigError):
                    validate_wiki_repository(value)

    def test_root_imports_work_outside_repository_cwd(self):
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import issuelens_config as host\n"
            "from github_app_mcp.src.issuelens_github_mcp import policy\n"
            "assert host.IssueLensConfigError is policy.IssueLensConfigError\n"
            "assert host.resolve_wiki_repository is policy.resolve_wiki_repository\n"
            "assert host.parse_config('version: 1') == {}\n"
        )
        with tempfile.TemporaryDirectory() as outside_root:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code], cwd=outside_root,
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    async def test_mixed_case_config_loads_configured_instruction(self):
        client = RepositoryClient({
            ".github/IssueLens.YML": (
                "version: 1\n"
                "instructions:\n"
                "  labeling:\n"
                "    path: .github/issuelens/labels.md\n"
            ),
            ".github/issuelens/labels.md": "Use the component labels.",
            ".github/label-instructions.md": "Legacy labels.",
        })

        result = await load_instruction(client, "microsoft/IssueLens", "labeling")

        self.assertEqual(result["configPath"], ".github/IssueLens.YML")
        self.assertEqual(result["source"], "configured")
        self.assertEqual(result["path"], ".github/issuelens/labels.md")
        self.assertEqual(result["content"], "Use the component labels.")

    async def test_missing_config_uses_legacy_label_instruction(self):
        client = RepositoryClient({
            ".github/label-instructions.md": "Use existing labels.",
        })

        result = await load_instruction(client, "microsoft/IssueLens", "labeling")

        self.assertEqual(result["configStatus"], "absent")
        self.assertEqual(result["source"], "legacy")
        self.assertEqual(result["path"], ".github/label-instructions.md")

    async def test_omitted_domain_uses_legacy_label_instruction(self):
        client = RepositoryClient({
            ".github/issuelens.yml": "version: 1\ninstructions: {}\n",
            ".github/label-instructions.md": "Use existing labels.",
        })

        result = await load_instruction(client, "microsoft/IssueLens", "labeling")

        self.assertEqual(result["configStatus"], "loaded")
        self.assertEqual(result["source"], "legacy")

    async def test_missing_config_uses_area_owner_search_order(self):
        client = RepositoryClient({
            "docs/area_owners.md": "Docs owners.",
            "area_owners.md": "Root owners.",
        })

        result = await load_instruction(client, "microsoft/IssueLens", "assignment")

        self.assertEqual(result["configStatus"], "absent")
        self.assertEqual(result["source"], "legacy")
        self.assertEqual(result["path"], "docs/area_owners.md")

    async def test_missing_config_and_legacy_file_use_built_in_behavior(self):
        result = await load_instruction(
            RepositoryClient({}),
            "microsoft/IssueLens",
            "criticality",
        )

        self.assertEqual(result["configStatus"], "absent")
        self.assertEqual(result["source"], "built-in")
        self.assertIsNone(result["content"])

    async def test_configured_planning_instruction_is_loaded(self):
        client = RepositoryClient({
            ".github/issuelens.yml": (
                "version: 1\ninstructions:\n  planning:\n"
                "    path: .github/issuelens/planning.md\n"
            ),
            ".github/issuelens/planning.md": (
                "Use maintainer-review and approved readiness states."
            ),
        })

        result = await load_instruction(
            client,
            "microsoft/IssueLens",
            "planning",
        )

        self.assertEqual(result["source"], "configured")
        self.assertEqual(result["path"], ".github/issuelens/planning.md")
        self.assertIn("maintainer-review", result["content"])

    async def test_missing_config_uses_built_in_planning_behavior(self):
        result = await load_instruction(
            RepositoryClient({}),
            "microsoft/IssueLens",
            "planning",
        )

        self.assertEqual(result["configStatus"], "absent")
        self.assertEqual(result["source"], "built-in")
        self.assertIsNone(result["content"])

    async def test_omitted_planning_domain_uses_built_in_behavior(self):
        result = await load_instruction(
            RepositoryClient({
                ".github/issuelens.yml": "version: 1\ninstructions: {}\n",
            }),
            "microsoft/IssueLens",
            "planning",
        )

        self.assertEqual(result["configStatus"], "loaded")
        self.assertEqual(result["source"], "built-in")
        self.assertIsNone(result["content"])

    async def test_configured_missing_instruction_fails_closed(self):
        client = RepositoryClient({
            ".github/issuelens.yml": (
                "version: 1\n"
                "instructions:\n"
                "  labeling:\n"
                "    path: .github/issuelens/missing.md\n"
            ),
        })

        with self.assertRaisesRegex(
            IssueLensConfigError,
            "Configured instruction file not found",
        ):
            await load_instruction(client, "microsoft/IssueLens", "labeling")

    async def test_ambiguous_config_filename_fails_closed(self):
        client = RepositoryClient({
            ".github/issuelens.yml": "version: 1\n",
            ".github/IssueLens.yml": "version: 1\n",
        })

        with self.assertRaisesRegex(IssueLensConfigError, "Multiple"):
            await load_instruction(client, "microsoft/IssueLens", "labeling")

    async def test_tool_returns_only_the_requested_domain(self):
        client = RepositoryClient({
            ".github/issuelens.yml": (
                "version: 1\n"
                "instructions:\n"
                "  criticality:\n"
                "    path: .github/issuelens/criticality.md\n"
                "  labeling:\n"
                "    path: .github/issuelens/labels.md\n"
            ),
            ".github/issuelens/criticality.md": "Core operations must work.",
            ".github/issuelens/labels.md": "Use component labels.",
        })
        tool = create_tool(client)

        result = await tool.handler(ToolInvocation(arguments={
            "repository": "microsoft/IssueLens",
            "domain": "criticality",
        }))

        self.assertEqual(result.result_type, "success")
        self.assertIn("Core operations must work.", result.text_result_for_llm)
        self.assertNotIn("Use component labels.", result.text_result_for_llm)

    async def test_tool_maps_invalid_config_to_safe_failure(self):
        tool = create_tool(RepositoryClient({
            ".github/issuelens.yml": "version: 2\n",
        }))

        result = await tool.handler(ToolInvocation(arguments={
            "repository": "microsoft/IssueLens",
            "domain": "labeling",
        }))

        self.assertEqual(result.result_type, "failure")
        self.assertIn("version must be", result.error)

    def test_parser_rejects_unknown_keys_and_unsafe_paths(self):
        invalid_configs = (
            "version: 1\nunknown: true\n",
            (
                "version: 1\ninstructions:\n  labeling:\n"
                "    path: ../labels.md\n"
            ),
            (
                "version: 1\ninstructions:\n  labeling:\n"
                "    path: https://example.com/labels.md\n"
            ),
            (
                "version: 1\ninstructions:\n  labeling:\n"
                "    path: .github\\labels.md\n"
            ),
        )

        for content in invalid_configs:
            with self.subTest(content=content):
                with self.assertRaises(IssueLensConfigError):
                    parse_config(content)

    def test_parser_rejects_aliases_multiple_documents_and_large_config(self):
        invalid_configs = (
            "version: &version 1\ninstructions: {}\n",
            "version: 1\n---\nversion: 1\n",
            "#" * (MAX_CONFIG_BYTES + 1),
        )

        for content in invalid_configs:
            with self.subTest(content=content[:40]):
                with self.assertRaises(IssueLensConfigError):
                    parse_config(content)

    def test_schema_and_sample_match_runtime_domains(self):
        schema = json.loads(
            (ROOT / "schemas" / "issuelens.schema.json").read_text(
                encoding="utf-8"
            )
        )
        schema_domains = set(
            schema["properties"]["instructions"]["properties"]
        )
        sample_paths = parse_config(
            (ROOT / "examples" / "issuelens.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(schema_domains, INSTRUCTION_DOMAINS)
        self.assertEqual(set(sample_paths), INSTRUCTION_DOMAINS)

    def test_current_repository_config_references_all_policy_files(self):
        config_path = ROOT / ".github" / "issuelens.yml"
        instruction_paths = parse_config(
            config_path.read_text(encoding="utf-8")
        )

        self.assertEqual(set(instruction_paths), INSTRUCTION_DOMAINS)
        for domain, relative_path in instruction_paths.items():
            with self.subTest(domain=domain):
                content = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertTrue(content.strip())
                self.assertLessEqual(
                    len(content.encode("utf-8")),
                    MAX_INSTRUCTION_BYTES,
                )
        for legacy_path in (
            ROOT / ".github" / "area_owners.md",
            ROOT / ".github" / "label-instructions.md",
        ):
            self.assertFalse(
                legacy_path.exists(),
                f"Current repository policy must use issuelens.yml: {legacy_path}",
            )


if __name__ == "__main__":
    unittest.main()
