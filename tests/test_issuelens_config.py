import json
import pathlib
import unittest

from copilot.tools import ToolInvocation

from issuelens_config import (
    INSTRUCTION_DOMAINS,
    MAX_CONFIG_BYTES,
    MAX_INSTRUCTION_BYTES,
    IssueLensConfigError,
    load_instruction,
    parse_config,
)
from issuelens_config_tool import create_tool


ROOT = pathlib.Path(__file__).parents[1]


class NotFoundError(RuntimeError):
    status_code = 404


class RepositoryClient:
    def __init__(self, files):
        self.files = files

    async def get_file(self, repository, path):
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
                "Use maintainer-review and go readiness states."
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
