from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
import re
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
TEXT_PARAMETERS = (
    "Protocol", "Release", "RunId", "TargetRepository",
    "TargetKind", "TargetNumber", "TargetRelationship",
)
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)
EVENT_NAMES = {
    "issuelens.run.started", "issuelens.run.completed", "issuelens.run.agent",
    "issuelens.run.model", "issuelens.run.target", "issuelens.run.error",
    "issuelens.request.rejected",
}
QUERY_GROUPS = {
    "Overview": {"overview-outcomes", "overview-usage", "overview-targets"},
    "Run explorer": {"run-explorer", "run-agents", "run-targets", "trace-requests"},
    "Reliability and performance": {
        "reliability-performance", "model-usage", "reliability-errors",
    },
    "Telemetry quality": {"telemetry-quality", "telemetry-incomplete-reasons"},
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def reject_nonfinite(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def strict_json(text):
    return json.loads(
        text, object_pairs_hook=unique_object, parse_constant=reject_nonfinite,
    )


def walk_items(items):
    for item in items:
        yield item
        if item["type"] == 12:
            yield from walk_items(item["content"]["items"])


def workbook_query(common, body):
    query = common + "\n" + body
    query = query.replace("let StartTime = ago(24h);\nlet EndTime = now();\n", "", 1)
    query = query.replace(
        "timestamp between (StartTime .. EndTime)", "timestamp {TimeRange}",
    )
    for parameter in TEXT_PARAMETERS:
        query = query.replace(
            f'let {parameter} = "";',
            f"let {parameter} = base64_decode_tostring('{{{parameter}:base64}}');",
            1,
        )
    return query


class TelemetryAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbook = strict_json(
            (ROOT / "observability" / "workbook.json").read_text(encoding="utf-8")
        )
        cls.catalog = (ROOT / "observability" / "queries.kql").read_text(encoding="utf-8")
        cls.documentation = (ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        common = re.findall(
            r"^// BEGIN COMMON\n(.*?)^// END COMMON$",
            cls.catalog, re.MULTILINE | re.DOTALL,
        )
        if len(common) != 1:
            raise ValueError("Expected exactly one COMMON block")
        cls.common = common[0].strip()
        blocks = re.findall(
            r"^// BEGIN QUERY ([a-z][a-z0-9-]+)\n(.*?)^// END QUERY$",
            cls.catalog, re.MULTILINE | re.DOTALL,
        )
        cls.queries = unique_object((name, body.strip()) for name, body in blocks)
        cls.items = list(walk_items(cls.workbook["items"]))
        cls.query_items = unique_object(
            (item["name"], item["content"]) for item in cls.items if item["type"] == 3
        )
        cls.parameters = unique_object(
            (parameter["name"], parameter)
            for item in cls.items if item["type"] == 9
            for parameter in item["content"]["parameters"]
        )

    def test_workbook_is_gallery_content_with_four_nonempty_views(self):
        self.assertEqual(self.workbook["version"], "Notebook/1.0")
        self.assertEqual(
            self.workbook["$schema"],
            "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
        )
        self.assertNotIn("resources", self.workbook, "This is not an ARM template")
        names = [item["name"] for item in self.items]
        self.assertEqual(len(names), len(set(names)))
        groups = [item for item in self.workbook["items"] if item["type"] == 12]
        self.assertEqual([group["content"]["title"] for group in groups], list(QUERY_GROUPS))
        for item in self.items:
            with self.subTest(item=item["name"]):
                self.assertIn(item["type"], {1, 3, 9, 12})
                self.assertIsInstance(item["content"], dict)
                if item["type"] == 1:
                    self.assertTrue(item["content"]["json"].strip())
                else:
                    self.assertEqual(item["content"]["version"], {
                        3: "KqlItem/1.0", 9: "KqlParameterItem/1.0", 12: "NotebookGroup/1.0",
                    }[item["type"]])
        for group in groups:
            content = group["content"]
            self.assertEqual(content["groupType"], "editable")
            queries = {item["name"] for item in content["items"] if item["type"] == 3}
            self.assertEqual(queries, QUERY_GROUPS[content["title"]])

    def test_parameters_have_valid_types_defaults_and_typed_target_options(self):
        self.assertEqual(set(self.parameters), set(TEXT_PARAMETERS) | {"Applications", "TimeRange"})
        ids = [parameter["id"] for parameter in self.parameters.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for parameter in self.parameters.values():
            with self.subTest(parameter=parameter["name"]):
                self.assertEqual(str(uuid.UUID(parameter["id"])), parameter["id"])
                self.assertEqual(parameter["version"], "KqlParameterItem/1.0")
                self.assertIsInstance(parameter["isRequired"], bool)
        applications = self.parameters["Applications"]
        self.assertEqual(applications["type"], 5)
        self.assertTrue(applications["isRequired"])
        self.assertEqual(
            applications["typeSettings"]["resourceTypeFilter"],
            {"microsoft.insights/components": True},
        )
        self.assertEqual(applications["value"], ["value::3"])
        timerange = self.parameters["TimeRange"]
        self.assertEqual(timerange["type"], 4)
        self.assertTrue(timerange["isRequired"])
        self.assertEqual(timerange["value"], {"durationMs": 86400000})
        for parameter in TEXT_PARAMETERS:
            self.assertEqual(self.parameters[parameter]["value"], "")
            self.assertFalse(self.parameters[parameter]["isRequired"])
        dropdowns = {
            "Protocol": {"", "invocations", "responses"},
            "TargetKind": {"", "repository", "issue", "pull_request", "work_item", "wiki"},
            "TargetRelationship": {"", "attempted", "read", "write_succeeded", "source"},
        }
        for name, expected in dropdowns.items():
            self.assertEqual(self.parameters[name]["type"], 2)
            options = strict_json(self.parameters[name]["jsonData"])
            self.assertEqual({option["value"] for option in options}, expected)
            self.assertEqual(len(options), len(expected))
            self.assertEqual([option["value"] for option in options if option.get("selected")], [""])
        for name in set(TEXT_PARAMETERS) - dropdowns.keys():
            self.assertEqual(self.parameters[name]["type"], 1)

    def test_every_workbook_query_matches_the_copyable_catalog(self):
        expected = set().union(*QUERY_GROUPS.values())
        self.assertEqual(set(self.queries), expected)
        self.assertEqual(set(self.query_items), expected)
        self.assertEqual(self.catalog.count("// BEGIN QUERY "), len(expected))
        self.assertEqual(self.catalog.count("// END QUERY"), len(expected))
        for name, body in self.queries.items():
            with self.subTest(query=name):
                self.assertMultiLineEqual(
                    self.query_items[name]["query"], workbook_query(self.common, body),
                )
                self.assertNotIn("{TimeRange}", body)
                self.assertNotIn(":base64}", body)

    def test_queries_bind_resource_time_and_all_filters(self):
        for name, content in self.query_items.items():
            with self.subTest(query=name):
                self.assertEqual(content["queryType"], 0)
                self.assertEqual(content["resourceType"], "microsoft.insights/components")
                self.assertEqual(content["crossComponentResources"], ["{Applications}"])
                self.assertEqual(content["timeContextFromParameter"], "TimeRange")
                self.assertEqual(content["visualization"], "table")
                self.assertIsInstance(content["size"], int)
                self.assertTrue(content["noDataMessage"])
                query = content["query"]
                self.assertIn("| where timestamp {TimeRange}", query)
                self.assertNotIn("StartTime", query)
                self.assertNotIn("EndTime", query)
                references = re.findall(r"\{([A-Za-z][A-Za-z0-9_]*)(?::([a-z0-9]+))?\}", query)
                self.assertEqual({parameter for parameter, _ in references}, set(TEXT_PARAMETERS) | {"TimeRange"})
                for parameter, formatter in references:
                    self.assertEqual(formatter, "" if parameter == "TimeRange" else "base64")
                for parameter in TEXT_PARAMETERS:
                    self.assertIn(
                        f"let {parameter} = base64_decode_tostring('{{{parameter}:base64}}');",
                        query,
                    )

    def test_free_text_parameter_substitution_cannot_add_kql_syntax(self):
        value = "'\n); union externaldata(x:string)[h@'https://example.invalid']; //"
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        for parameter in TEXT_PARAMETERS:
            with self.subTest(parameter=parameter):
                declaration = next(
                    line for line in self.query_items["overview-usage"]["query"].splitlines()
                    if line.startswith(f"let {parameter} = ")
                )
                expanded = declaration.replace(f"{{{parameter}:base64}}", encoded)
                match = re.fullmatch(
                    rf"let {parameter} = base64_decode_tostring\('([A-Za-z0-9+/=]*)'\);",
                    expanded,
                )
                self.assertIsNotNone(match)
                self.assertEqual(base64.b64decode(match[1]).decode("utf-8"), value)

    def test_schema_version_and_tokens_match_runtime_constants_without_imports(self):
        module = ast.parse((ROOT / "telemetry.py").read_text(encoding="utf-8"))
        constants = {
            target.id: node.value
            for node in module.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertEqual(ast.literal_eval(constants["SCHEMA_VERSION"]), "1")
        self.assertEqual(ast.literal_eval(constants["TOKEN_FIELDS"]), TOKEN_FIELDS)
        self.assertIn('| where tostring(d.schema_version) == "1"', self.common)
        self.assertEqual(set(re.findall(r'"(issuelens\.[a-z.]+)"', self.common)), EVENT_NAMES)
        self.assertIn("customEvents", self.common)
        self.assertIn("d = customDimensions", self.common)
        self.assertNotIn("AppEvents", self.common)
        self.assertNotIn('d["issuelens.', self.catalog)

    def test_fact_properties_use_only_the_documented_flat_allowlist(self):
        expected = {
            "schema_version", "run_id", "protocol", "release",
            "run_trace_id", "invocation_id", "response_id", "conversation_id", "session_id",
            "repository", "repository_id", "target_kind", "number", "relationship", "operations",
            "agent_run_id", "parent_agent_id", "role", "status", "model",
            "transport_status", "execution_status", "business_outcome", "admitted",
            "model_request_sent", "stage", "error_type",
            "duration_s", "first_root_output_s", "first_final_output_s",
            "usage_status", "usage_calls", "tools_started", "tools_completed",
            "tools_failed", "agents_started", "model_failures", "model_retries",
            "write_operations_succeeded", "notification_submissions",
            "context_tokens_peak", "context_token_limit", "attribution_complete",
            "telemetry_incomplete",
        } | set(TOKEN_FIELDS) | {f"{field}_calls" for field in TOKEN_FIELDS}
        self.assertEqual(set(re.findall(r"\bd\.([a-z_][a-z0-9_]*)", self.catalog)), expected)
        for body in self.queries.values():
            self.assertNotRegex(body, r"\|\s*project\s+\*")
            self.assertNotRegex(body, r"\|\s*project(?:[^\n]*,\s*)?d\s*(?:$|,)")
            self.assertNotRegex(body, r"\b(?:message|url|outerMessage|innermostMessage)\b")

    def test_all_fact_grains_are_deduplicated_before_reporting(self):
        expected_grains = {
            "Starts": "run_id",
            "Runs": "run_id",
            "Agents": "run_id, agent_run_id",
            "Models": "run_id, model",
            "TargetFacts": "run_id, repository, target_kind, number, relationship",
        }
        for table, grain in expected_grains.items():
            with self.subTest(table=table):
                definition = re.search(
                    rf"let {table} = materialize\((.*?)\);",
                    self.common, re.DOTALL,
                )
                self.assertIsNotNone(definition)
                self.assertIn(f"summarize arg_max(timestamp, *) by {grain}", definition[1])
        self.assertIn("Starts | join kind=leftanti (Runs | project run_id) on run_id", self.common)
        errors = self.queries["reliability-errors"]
        self.assertIn("| distinct run_id, protocol, release, stage, error_type", errors)
        self.assertLess(errors.index("| distinct "), errors.index("| summarize "))

    def test_target_matching_is_typed_and_cannot_multiply_run_totals(self):
        self.assertIn("repository = tolower(tostring(d.repository))", self.common)
        self.assertIn('TargetKind in ("issue", "pull_request", "work_item")', self.common)
        self.assertIn('TargetKind in ("repository", "wiki") and TargetNumber == "0"', self.common)
        self.assertIn('TargetNumber matches regex @"^[1-9][0-9]*$"', self.common)
        self.assertIn("isnotempty(TargetRepository) and TargetKind", self.common)
        self.assertIn("| where ValidTargetNumber", self.common)
        self.assertIn("| where isempty(TargetKind) or target_kind == TargetKind", self.common)
        self.assertIn("| where isempty(TargetNumber) or tostring(number) == TargetNumber", self.common)
        self.assertIn("| distinct run_id;\nlet Facts", self.common)
        self.assertIn("not(FilterByTarget) or run_id in (MatchingRuns)", self.common)
        targets = self.queries["overview-targets"]
        self.assertIn(
            "Targets | distinct repository, target_kind, number, relationship | summarize exact_recorded_identities = count()",
            targets,
        )
        self.assertNotRegex(self.catalog, r"\bdcount(?:if)?\(")
        for name in ("overview-usage", "model-usage", "reliability-performance"):
            self.assertNotIn("join", self.queries[name])
            self.assertNotIn("Targets", self.queries[name])

    def test_token_totals_preserve_nulls_and_include_per_field_coverage(self):
        for name, table in (("overview-usage", "Runs"), ("model-usage", "Models")):
            body = self.queries[name]
            with self.subTest(query=name):
                self.assertTrue(body.startswith(table + "\n"))
                self.assertNotIn("Agents", body)
                self.assertNotIn("union", body)
                self.assertNotIn("coalesce(", body)
                self.assertIn("reported_usage_calls = sum(tolong(d.usage_calls))", body)
                for field in TOKEN_FIELDS:
                    samples = field.removesuffix("_tokens") + "_samples"
                    self.assertIn(f"{samples} = countif(isnotnull({field}))", body)
                    self.assertIn(f"reported_{field}_calls = sum(tolong(d.{field}_calls))", body)
                    self.assertIn(f"observed_{field} = sum({field})", body)
                    self.assertIn(
                        f"observed_{field} = iff({samples} > 0, observed_{field}, long(null))",
                        body,
                    )
                self.assertNotIn("context_tokens", body)
        self.assertIn('| where name == "issuelens.run.completed"', self.queries["overview-usage"])
        self.assertNotRegex(self.catalog, r"\bsum\([^)]*\+")

    def test_latency_percentiles_have_known_sample_counts_and_no_zero_fallback(self):
        body = self.queries["reliability-performance"]
        for field, samples in (
            ("duration_s", "duration_samples"),
            ("first_root_output_s", "root_output_samples"),
            ("first_final_output_s", "final_output_samples"),
        ):
            self.assertIn(f"{samples} = countif(isnotnull({field}))", body)
            for percentile in (50, 95, 99):
                self.assertIn(f"percentile({field}, {percentile})", body)
        self.assertIn("root_output_coverage_pct", body)
        self.assertIn("final_output_coverage_pct", body)
        self.assertNotIn("coalesce(", body)
        self.assertNotIn("time_to_first_token", body)
        for field in ("tools_failed", "model_failures", "model_retries"):
            self.assertIn(f"{field}_samples = countif(isnotnull({field}))", body)

    def test_quality_keeps_rejections_unknown_ownership_and_ingestion_gaps_separate(self):
        for state in (
            "rejection_with_start", "admission_rejection", "paired",
            "terminal_without_start_in_window", "start_without_terminal_in_window",
        ):
            self.assertIn(f'"{state}"', self.common)
            self.assertIn(f'"{state}"', self.queries["telemetry-quality"])
        quality = self.queries["telemetry-quality"]
        for field in (
            "usage_complete", "usage_partial", "usage_unavailable", "usage_not_applicable",
            "missing_usage_status", "incomplete_attribution", "missing_attribution_state",
            "incomplete_telemetry", "missing_completeness_state",
            "unknown_transport_status", "missing_transport_status",
            "model_requests_sent", "model_not_sent", "missing_model_request_state",
            "ingestion_lag_samples", "negative_ingestion_lag_samples",
        ):
            self.assertIn(field + " =", quality)
        self.assertIn("attribution_complete == false", quality)
        self.assertIn("isnull(attribution_complete)", quality)
        self.assertIn("isnull(telemetry_incomplete)", quality)
        self.assertIn("ingestion_time()", self.common)
        self.assertIn("(ingested_at - timestamp) / 1s", quality)
        self.assertIn("iff(raw_lag_s >= 0, raw_lag_s, real(null))", quality)
        reasons = self.queries["telemetry-incomplete-reasons"]
        self.assertTrue(reasons.startswith("Runs\n"))
        self.assertIn('where reason startswith "incomplete_"', reasons)
        self.assertIn('reason endswith "_limit", "bounded_state_limit"', reasons)
        self.assertIn("affected_runs = count()", reasons)
        self.assertIn("reported_occurrences = sum(observed_count)", reasons)

    def test_transport_execution_business_and_model_dispatch_are_independent(self):
        outcomes = self.queries["overview-outcomes"]
        for field in ("transport_status", "execution_status", "business_outcome"):
            self.assertIn(f"{field} = tostring(d.{field})", outcomes)
            self.assertIn(f'{field} = iff(isempty({field}), "unknown", {field})', outcomes)
        self.assertIn("release, transport_status, execution_status, business_outcome", outcomes)
        explorer = self.queries["run-explorer"]
        self.assertIn(
            'transport_status = iff(terminal_seen and isnotempty(tostring(d.transport_status)), '
            'tostring(d.transport_status), "unknown")', explorer,
        )
        self.assertIn("model_request_sent = tobool(d.model_request_sent)", explorer)
        quality = self.queries["telemetry-quality"]
        for condition in ("model_request_sent == true", "model_request_sent == false", "isnull(model_request_sent)"):
            self.assertIn(f"countif(terminal_seen and {condition})", quality)
        self.assertNotIn("coalesce(", quality)
        self.assertIn("duration_s = todouble(d.duration_s)", self.queries["run-agents"])
        self.assertIn("repository_id = tolong(d.repository_id)", self.queries["run-targets"])
        self.assertNotIn("repository_id", self.queries["overview-targets"])
        self.assertNotIn("repository_id", self.common, "Optional enrichment must not split logical target keys")

    def test_run_explorer_and_native_trace_links_preserve_correlation(self):
        explorer = self.queries["run-explorer"]
        for identifier in (
            "run_id", "run_trace_id", "invocation_id", "response_id",
            "conversation_id", "session_id",
        ):
            self.assertIn(identifier, explorer)
        self.assertIn('iff(terminal_seen, tostring(d.execution_status), "unknown")', explorer)
        agents = self.queries["run-agents"]
        self.assertIn("where isnotempty(RunId)", agents)
        self.assertIn("parent_agent_id = tostring(d.parent_agent_id)", agents)
        self.assertIn("role = tostring(d.role)", agents)
        self.assertIn("exclusive_input_tokens = tolong(d.input_tokens)", agents)
        traces = self.queries["trace-requests"]
        self.assertIn("where isnotempty(RunId)", traces)
        self.assertIn("| distinct run_trace_id;", traces)
        self.assertIn("where operation_Id in (TraceIds)", traces)
        self.assertIn("| summarize arg_max(timestamp, *) by itemId", traces)
        self.assertIn("| project timestamp, operation_Id, id, itemId, duration, success, resultCode", traces)
        formatter, = self.query_items["trace-requests"]["gridSettings"]["formatters"]
        self.assertEqual(formatter["columnMatch"], "itemId")
        self.assertEqual(formatter["formatter"], 7)
        self.assertEqual(formatter["formatOptions"]["linkTarget"], "RequestDetails")
        self.assertTrue(formatter["formatOptions"]["linkIsContextBlade"])
        self.assertNotIn("https://portal.azure.com", traces, "Use native links, not invented portal URLs")

    def test_documentation_and_readme_links_resolve_inside_repository(self):
        for relative in ("docs/observability.md", "observability/workbook.json", "observability/queries.kql"):
            self.assertIn(f"]({relative})", self.readme)
            self.assertTrue((ROOT / Path(relative)).is_file())
        self.assertEqual(self.readme.count("## Observability\n"), 1)
        for link in re.findall(r"\]\(([^)]+)\)", self.documentation):
            if "://" in link or link.startswith("#"):
                continue
            path = (ROOT / "docs" / Path(link.split("#", 1)[0])).resolve()
            self.assertTrue(path.is_relative_to(ROOT))
            self.assertTrue(path.is_file(), link)

    def test_documentation_states_scope_privacy_and_unvalidated_live_boundaries(self):
        for required in (
            "ISSUELENS_RELEASE",
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
            "AppEvents", "TimeGenerated", "Properties", "customEvents",
            "unsampled", "not an audit ledger", "never metric labels",
            "CLI OTLP/collector route is follow-on", "No live Workbook import",
            "not an ARM deployment template", "not compile KQL or contact Azure",
            "HTTP success is not business success", "not model TTFT",
            "not zero tokens", "not estimates of all usage", "not delivery",
            "No model prices", "no configured alert rules or invented SLO",
            "Telemetry is always on for both protocols",
            "Both hosted manifests also explicitly set `false`",
            "Forced to `false` before host initialization, including an inherited `true`",
            "by default, so it remains `unknown`",
            "not export native per-call CLI traces",
            "explicit invalid span",
            "host log sampling processors",
            "not an assurance of exact full attribution",
            "no extra reads", "merges pending unknown attempted/read associations",
            "model_request_sent", "transport_status", "repository_id",
            "test_telemetry_assets.py",
        ):
            with self.subTest(text=required):
                self.assertIn(required.casefold(), " ".join(self.documentation.split()).casefold())
        workbook_text = json.dumps(self.workbook)
        self.assertNotIn("InstrumentationKey=", workbook_text)
        self.assertNotIn("APPLICATIONINSIGHTS_CONNECTION_STRING", workbook_text)


if __name__ == "__main__":
    unittest.main()
