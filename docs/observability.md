# IssueLens operational reporting

This is the reporting MVP for [feature #33](https://github.com/microsoft/IssueLens/issues/33):
content-free, schema-versioned operational facts and an Azure Monitor Workbook.
It covers one `/invocations` request or one `/responses` turn per `run_id`,
not a whole conversation. Collection captures activity from the running service; it
does not recover historical SDK usage events.

**Validation boundary:** the asset tests validate JSON structure and KQL
consistency offline. No live Workbook import, KQL execution, telemetry query,
model call, or deployment is established by these assets. Import and ingestion
must still be checked against the intended nonproduction resource and deployed
package versions with separate authorization. Editing this guide or importing
a Workbook does not authorize deployment of an agent.

## Setup and privacy

Use the existing Foundry host telemetry pipeline and its configured Application
Insights resource. Do not create a second global OpenTelemetry provider.
Telemetry is always on for both protocols; there is no application-level
enable/disable setting. Check the intended telemetry destination and access
policy before an approved deployment.

| Setting | Meaning |
| --- | --- |
| `ISSUELENS_RELEASE` | Bounded identifier, default `unknown`; use a stable release or commit identifier, not a user/task value. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Owned by Foundry when hosted. Configure project monitoring; **do not override this reserved variable in a deployment manifest** or put it in Workbook JSON. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Forced to `false` before host initialization, including an inherited `true`. Both hosted manifests also explicitly set `false`. |

Release identifiers use `[A-Za-z0-9_.-]`, with 1-64 characters.
Neither hosted manifest injects `ISSUELENS_RELEASE`
by default, so it remains `unknown` unless supplied in the deployment
environment. For an approved deployment, add an explicit bounded value to
`agent.yaml`'s `environment_variables` or the service's `environmentVariables`
in `azure.yaml`, as appropriate for the deployment path:

```yaml
- name: ISSUELENS_RELEASE
  value: release-2026-09
```

The separate native Copilot CLI exporter is disabled
to avoid duplicate telemetry. A CLI OTLP/collector route is follow-on work, not
an additional prerequisite or an already-verified integration. This MVP does
not export native per-call CLI traces. Event-derived diagnostic spans and
retained host requests do not establish full per-call trace coverage.

The allowlisted facts contain no raw prompts, answers, reasoning, issue bodies,
source/file/image contents, tool arguments/results, error messages, credentials,
email addresses, or URL query strings. Repository names/IDs, issue/PR numbers,
conversation/session/run IDs and trace IDs are still sensitive metadata: restrict
reader access and retention to the repositories' intended audience. They belong
in logs/spans, **never metric labels**. Only bounded dimensions such as
protocol, supported role/operation and model are suitable for
metrics. Do not paste production identifiers into public support tickets or
publish exported Workbook results without checking their audience.

Operational facts must remain **unsampled**, independently of diagnostic trace
sampling, including trace-based log sampling and ingestion-side settings.
The SDK emitter submits facts inside an isolated OpenTelemetry context with an
explicit invalid span and no inherited baggage, retaining `run_trace_id`
instead of inheriting a diagnostic trace's sampling flags. An empty `Context`
alone is insufficient: the SDK can fall back to the active trace, and hosting
processors can read ambient baggage rather than the record's context.
Offline tests exercise the installed host log sampling processors, hosting
enrichment and Azure custom-event conversion: ordinary unsampled diagnostic
logs are dropped while the independent BI fact remains. This does not verify
Azure ingestion settings, network delivery, retention, or live query results.
Full traces may be sampled. Unsampled does not mean lossless: exporter queue
limits, shutdown, ingestion failures and retention can lose data. Application
Insights is not an audit ledger or an exactly-once record of GitHub changes.
Exporter failures never authorize retries of model calls or business actions.
Unexpected setup failures escape as sanitized errors,
so automatic host exception recording cannot copy their original messages.
Notification HTTP instrumentation is suppressed to avoid recording SAS URLs.
Same-conversation overlapping chat turns are rejected rather
than mixing their event accounting; clients receive an explicit retry-later
message. Different conversations remain concurrent.

The offline compatibility baseline is Copilot SDK 1.0.7, hosted core 2.1.0,
Microsoft OpenTelemetry 1.3.9, and OpenTelemetry API/SDK 1.44.0. The dependency
minimums preserve this baseline. Re-run the exporter/host contract tests when
upgrading: private hosting processor behavior and SDK event schemas can change.

## Import the Workbook

1. In the intended **Application Insights resource**, open **Workbooks**, create
   a new Workbook, choose **Edit**, then **Advanced Editor**.
2. Use the **Gallery Template** representation and replace its JSON with
   [`observability/workbook.json`](../observability/workbook.json). This is
   `Notebook/1.0` Workbook content, **not an ARM deployment template**.
3. Apply it, select the intended **Applications** resource and a short time
   range, and review the filters. The default resource selection is the
   Workbook's Application Insights context; do not accidentally select unrelated
   resources. Saving a Workbook requires the appropriate Azure permission.
4. After separately authorized nonproduction ingestion, verify both protocols,
   known event names, stringified dimensions, usage coverage, and trace links.
   A successfully rendered empty grid is not evidence that ingestion works.

The four views are:

| View | Reports |
| --- | --- |
| Overview | Daily terminal transport/execution/business outcomes, observed token totals with coverage, and exact recorded target identities by relationship. |
| Run explorer | Latest 100 lifecycle rows, run/trace and optional host/session IDs, per-agent exclusive facts, up to 500 target rows, and retained request trace links for one selected run. |
| Reliability and performance | Per-release/protocol duration and first-output p50/p95/p99 with sample counts, observed tool/model failures and retries, model usage, and bounded error categories. |
| Telemetry quality | Missing lifecycle counterparts, incomplete usage/ownership, missing completeness flags, reported bounded-state limits, and ingestion-lag percentiles with sample counts. |

Time range, protocol and release apply to every report. Blank
text filters mean all values; release matching is exact. `RunId`
selects one run and enables its agent and request-detail panels. The Workbook
base64-encodes text substitutions before KQL decodes them, so quotes or newlines
in a filter cannot become query syntax. Filters are not an authorization boundary.

### Repository and work-item lookup

`TargetRepository` is a case-insensitive canonical `owner/repository`, not a URL.
Use `TargetKind=issue` or `pull_request` and a positive `TargetNumber`; the same
number must never silently match both kinds. Unresolved types remain
`work_item`, not assumed issues. A number filter requires both repository and
kind; invalid combinations return no matching rows. For repository/wiki
identities the number is `0`. `TargetRelationship` can restrict the match to
`attempted`, `read`, `write_succeeded`, or `source`.

**Target filters select runs, not allocations.** All facts and all associated
targets of a matching run remain visible. Its root token total is included
once, not charged independently to each target. Do not add separate repository
filtered totals together: a multi-target run can appear in several selections.
A target filter also hides runs whose target facts are missing, so clear it
when investigating telemetry loss.

Successful host issue-image reads contribute `read` associations as unresolved
`work_item` identities. A later successful typed `get_issue` or
`get_pull_request` SDK result resolves the issue/PR kind and merges pending
unknown attempted/read associations into the known identity before export,
rather than counting the same observed item twice. Without that evidence the
kind stays `work_item`; the reports do not guess a type or retrospectively
rewrite facts from other runs.

Successful in-process `issuelens-config` calls count as repository reads.
`get_repository` can supply an optional numeric `repository_id` when already
observed; telemetry makes no extra reads to obtain it. The target detail grid
shows that ID when known, but keeps canonical repository/kind/number/relationship
as its logical key so optional enrichment cannot split one association.

Tool attempts are not proven primary user targets. Reads are execution evidence,
not proof every item returned by a list/search was analyzed. `write_succeeded`
means an observed successful operation, not necessarily a state change: an
idempotent label call need not add a new label. Wiki `source` repository and
destination `wiki` identities are separate. Unresolved observations in separate
runs cannot be assumed to be either issues or PRs.
Wiki writes additionally require the tool's `updated` or `no-change` status;
transport success with an unknown status is incomplete evidence, not a
confirmed write.
For `add_eyes_reaction`, `target_id` identifies an issue/PR number only when
`target_kind` is `issue` or `pull_request`. Reactions to comments remain
repository-level associations; their comment IDs are not issue/PR numbers.

## Logical schema and table scope

The tested logical query schema is **Application Insights `customEvents`**:
`timestamp`, `name`, and flat `customDimensions`, with
`customDimensions.schema_version == "1"`. Numeric/Boolean dimensions are cast
explicitly because Application Insights may store them as strings. Dotted
span attributes such as `issuelens.run_id` are not the fact field `run_id`.

| Event name | Logical grain / interpretation |
| --- | --- |
| `issuelens.run.started` | One admitted run; includes observed SDK/host version metadata when available. |
| `issuelens.run.completed` | One admitted run's terminal snapshot, including failures and cancellations. The event name alone does **not** mean successful execution. |
| `issuelens.request.rejected` | Admission failure, separate from admitted runs. A matching start is not expected. |
| `issuelens.run.agent` | One `(run_id, agent_run_id)` summary; parent ID, role, status, exclusive usage/tool counts and captured `duration_s`. |
| `issuelens.run.model` | One `(run_id, model)` usage aggregate, not one model-call trace. |
| `issuelens.run.target` | One `(run_id, repository, target_kind, number, relationship)` association with observed operation count and optional `repository_id`. |
| `issuelens.run.error` | Bounded `stage`/`error_type` observations, not raw exceptions. The error report counts affected runs per category, not distinct attempts. |

Common dimensions are `schema_version`, `run_id`, `protocol` (`invocations` or
`responses`), `release`, `run_trace_id`, and optional
`invocation_id`, `response_id`, `conversation_id`, `session_id`. Unknown
identifiers remain blank. Agent roles are `issuelens`, `triage`, `plan`,
`find-criticals`, `team-memory`, or `unknown`; unknown ownership is not assigned
to a plausible role by the reports.
The root agent fact is also emitted for a rejected request or a no-model
greeting; it is not proof that inference ran. Check the terminal `admitted`
and `model_request_sent` fields.

The terminal snapshot includes:

| Fields | Interpretation |
| --- | --- |
| `transport_status` | Host-observed transport state: `completed`, `interrupted`, `rejected`, `unknown`. Independent of execution and business effects. |
| `execution_status`, `business_outcome`, `admitted` | Independent execution, observed business effect and admission state. Status: `completed`, `failed`, `cancelled`, `rejected`. Outcome: `unknown`, `no_action`, `confirmed_operations`, `partial`. |
| `model_request_sent` | Boolean indicating the host attempted to send the model turn; not proof the provider accepted it, nor a count of individual model API calls. Missing values remain unknown. |
| `duration_s`, optional `first_root_output_s`, `first_final_output_s` | Host-observed elapsed seconds; missing observations stay null. |
| `usage_status`, `usage_calls`, token fields and `<tokenfield>_calls` | Live usage observations and per-field coverage. Status: `complete`, `partial`, `unavailable`, `not_applicable`. |
| `tools_started`, `tools_completed`, `tools_failed`, `model_failures`, `model_retries` | Observed lifecycle counts, not inferred from model narration. |
| `agents_started` | Distinct tracked subagents, excluding the root and synthetic `unattributed` bucket. The bucket retains unknown usage, but is not a distinct observed subagent. |
| `write_operations_succeeded`, `notification_submissions` | Confirmed operation/submission observations, not guaranteed state changes or downstream delivery. |
| `attribution_complete`, `telemetry_incomplete`, `incomplete_<boundedreason>` | Ownership and collection quality; absent flags are unknown, not false. |
| Optional `context_tokens_peak`, `context_token_limit` | Context pressure from `session.usage_info`, not billable token usage. |

### Log Analytics mapping

When Logs is scoped to the underlying Log Analytics workspace, the equivalent
table can be `AppEvents`. Do **not** union `AppEvents` and `customEvents`:
they can be two names/views of the same data. Replace the `customEvents` source
in the COMMON block with:

```kusto
AppEvents
| where _ResourceId =~ "/subscriptions/<subscription>/resourceGroups/<group>/providers/microsoft.insights/components/<app>"
| project timestamp = TimeGenerated, name = Name, customDimensions = Properties
```

Keep the rest of the query unchanged, and explicitly select the intended
Application Insights resource ID. The delivered Workbook itself uses the
Application Insights scope; it does not auto-detect workspace tables. Its
optional request-trace panel is also Application Insights scoped, not a
workspace-native trace-link implementation. `AppRequests` uses names such as
`TimeGenerated`, `OperationId` and `DurationMs`; adapting that panel's
`itemId`-based native link needs resource-specific validation rather than
assuming its portal identity is the span `Id`.

Table/column availability, ingestion-time policy and portal detail links need
live verification in the chosen resource. The mapping above is an instruction
for adapting the logical fact schema, not a claim of a tested cloud query.

## Copyable KQL and accounting rules

In [`observability/queries.kql`](../observability/queries.kql), copy the block
between `BEGIN COMMON` / `END COMMON`, followed immediately by **one** block
between `BEGIN QUERY <name>` / `END QUERY`. Edit the initial literals and run
both together in Logs. Do not run the whole file as one report. Start with
`telemetry-quality`, then `overview-outcomes` or `run-explorer`. Workbook query
bodies and these blocks are kept in sync by the offline tests.

Every logical start/terminal row is deduplicated by `run_id`; agent/model/target
rows use their full keys above before aggregation. Target selection uses a
distinct run-ID set rather than a many-to-many join. Token totals use **only
root terminal facts** in `overview-usage`, or **only model facts** in
`model-usage`. Never add the two reports together, or add parent inclusive totals
to child exclusive totals. Agent facts are an ownership drill-down, not another
copy of the run ledger. Missing model facts can prevent reconciliation.
Exclusive attribution describes observed events, not an assurance of exact
full attribution when events or ownership data are missing. Even
`attribution_complete=true` does not prove that all execution events were
retained; inspect usage coverage and `telemetry_incomplete` alongside it.

Target reports use `distinct` then `count()` for exact counts of **observed
typed identities**; they do not use approximate `dcount()`. Relationship rows,
`any_repository`, and individual target kinds overlap and are not additive.
These counts cannot reconstruct lost target facts or prove a complete inventory.

### Usage and missing values

Token fields are `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, and `reasoning_tokens`. Live SDK `assistant.usage` events
are ephemeral; session history and cumulative context counters do not restore
missing per-call usage. Retries may consume usage without a successful result.
`usage_calls` means observed usage events/calls, not every attempted model call.

`complete` describes the adapter's required input/output accounting; it does
not assert that optional cache/reasoning fields were exposed by the provider.
`<tokenfield>_calls` is the number of observed calls that reported that field.
Compare each `reported_*_calls` numerator with `reported_usage_calls`; a zero
coverage numerator means no such field was reported, **not zero tokens**.
The `*_samples` columns count records with a known token total. Aggregates
return null when no sample has that field, even though KQL `sum()` alone would
return zero. With partial coverage, displayed sums are only observed totals,
not estimates of all usage. No gap-filling or success-shaped fallback is used.

Cache/reasoning breakdowns may already be included in input/output totals.
Do not add them into a new grand total. No model prices, currency, Copilot
billing conversion, cost estimates or cost alerts are implemented here.

### Metrics and diagnostic spans

The existing host meter provider also receives these low-cardinality instruments:

| Instrument | Meaning |
| --- | --- |
| `issuelens.run.count` | Terminal requests by protocol, status and admission. |
| `issuelens.run.active` | Process-local in-flight admitted runs; an up/down counter, not a count to sum across time. |
| `issuelens.run.duration`, `issuelens.run.time_to_first_output` | Host-observed run and first-root-output histograms, in seconds. |
| `issuelens.host.duration` | Observed client/session/media setup and cleanup phase durations. |
| `issuelens.model.time_to_first_token` | Provider-reported model TTFT when exposed by the live SDK event, in seconds. |
| `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` | Reported input/output tokens and model-call durations. |
| `gen_ai.execute_tool.duration`, `gen_ai.invoke_agent.duration` | Observed tool and subagent durations, including failure status. |
| `issuelens.model.retries`, `issuelens.model.failures`, `issuelens.telemetry.incomplete` | Observed retries, failures and bounded incompleteness reasons. |

GenAI instruments use `gen_ai.request.model`, `gen_ai.token.type`,
`gen_ai.operation.name`, `gen_ai.agent.name` and other applicable semantic
dimensions. No run/repository/work-item identifiers are metric dimensions.
Use Metrics Explorer for the instruments; availability depends on the host
exporter. Do not derive per-call percentiles from already aggregated metric
averages. Workbook first-output percentiles use individual run facts instead.

Diagnostic spans describe the run, host phases, subagents, tools and timed
model attempts. They preserve explicit parent trace correlation without
copying arbitrary request baggage or host-span attributes. Logical role stays
in `issuelens.agent.role` because the hosting runtime can overwrite
`gen_ai.agent.*` with the deployed agent's identity. Unknown or ambiguous
delegation ownership remains explicitly incomplete; parent/child inclusive
token summaries are never added to exclusive usage.

### Timing, status and traces

`duration_s` is host-observed run time. `first_root_output_s` measures the first
nonempty, user-facing root-agent text actually emitted by the protocol handler;
lifecycle events, reasoning, tool traffic and nested-agent text do not establish
this value. `first_final_output_s` is the first emitted final-phase text,
**not** completion of the full answer. Neither includes client queues, network
transit or rendering.

These are **not model TTFT**. The adapter may observe provider TTFT as a metric,
but this Workbook does not invent per-call TTFT from run/model facts. Missing
first output remains null. KQL percentiles ignore nulls; every timing report
shows the known sample counts and first-output coverage. Agent summaries now
capture `duration_s` when the observed execution ends; older/missing values
remain nullable in these reports. Overlapping child durations
must not be summed and described as wall-clock run duration.

HTTP success is not business success. `requests.success`,
`transport_status=completed`, a finished SSE stream, and
`execution_status=completed` do not prove correct triage or successful
publication. A completed transport may carry a failed execution.
`business_outcome=unknown` is not `no_action`; a partial/failed run
may already have confirmed operations. Notification submission is not delivery.

For correlation, copy a `run_id` into `RunId` and inspect its `run_trace_id`,
host IDs, exclusive agent rows and typed targets. The **Retained request traces**
grid matches `requests.operation_Id` to `run_trace_id`; its **Details** links
use Application Insights' native request-detail view (`itemId`). Open related
dependencies/spans from that view, or search `dependencies.operation_Id` for
the same trace ID. Do not treat an individual HTTP row as the run's result.
The grid deliberately does not select URLs, request names, exception text or
trace messages. Because metadata facts use an isolated, trace-independent context,
their own automatic `operation_Id` is not the correlation contract: use the
explicit `run_trace_id`. A native portal Details link is not a promise of
native per-call CLI trace export.

No retained request row means only that this panel found none in the selected
scope/window. The run may still have dependencies, a sampled-out trace, delayed
ingestion or a correlation gap. A populated `run_trace_id` does not prove the
full trace was retained. There is no verified Foundry-specific deep link in
this MVP.

## Operating telemetry quality

Check quality before interpreting low counts or unusually good latency.
`start_without_terminal_in_window` can be an active run, an interrupted process,
a lost completion, delayed ingestion, or the selected window cutting through a
run. The converse can reflect a start before the window, or a lost start.
Broaden the time range, clear target filters and allow for ingestion before
classifying loss. Rejected requests need no start; missing completion is never
silently classified as success, failure, or zero usage.

`telemetry-quality` counts missing usage/attribution/completeness states,
unknown/missing transport status, and missing model-dispatch state separately.
`model_request_sent=false` is distinct from a missing flag and from a sent turn
whose usage events are unavailable. The agent detail panel preserves
`role=unknown` and parent IDs for
ownership investigation. `telemetry-incomplete-reasons` expands only
`incomplete_` counters: `*_limit` reasons identify bounded-state limits, while
other reasons can identify invalid/missing events, unfinished tools/agents,
uncertain ownership, missing wiki identity or rejected export attempts. Counts
describe reported observations, not an exact number of lost records. A failed
export of the final fact cannot report its own loss in that fact.

Ingestion lag uses `ingestion_time() - timestamp` on the deduplicated terminal
fact, or the start when no terminal is observed. It is not client latency.
Unknown ingestion times stay null; negative samples are counted separately and
excluded from percentiles. An empty report or an apparently clean quality flag
does not rule out exporter/ingestion loss; inspect the platform's delivery
diagnostics and retained lifecycle evidence as well.

There are no configured alert rules or invented SLO thresholds in these assets.
After an authorized pilot establishes coverage and a baseline, owners can
define alert windows/thresholds for sustained execution failures, latency
regressions and telemetry gaps. Pricing, richer dependency detail, client
timing, native CLI collector export, Power BI, and human-rated triage/planning
quality remain follow-on work.

## Offline checks and references

From the repository root on Windows, using the existing environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_telemetry_assets.py -v
```

The asset tests use `unittest` and the Python standard library. They check Workbook
structure/parameters/native trace-link wiring, query identity with the KQL
catalog, schema fields, deduplication, null-preserving aggregation and
documentation links. They do not compile KQL or contact Azure.
The adjacent `test_telemetry.py`, `test_host_telemetry.py` and
`test_telemetry_export.py` suites cover live-event accounting, protocol
lifecycles, actual SDK wire decoding, hosting enrichment, trace-based log
sampling, custom-event conversion and content canaries using offline providers.

- [Workbook JSON schema](https://github.com/microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json)
- [Workbook parameters and safe base64 formatting](https://learn.microsoft.com/en-us/azure/azure-monitor/visualize/workbooks-parameters)
- [Workbook resource parameters](https://learn.microsoft.com/en-us/azure/azure-monitor/visualize/workbooks-resources)
- [Workbook native link actions](https://learn.microsoft.com/en-us/azure/azure-monitor/visualize/workbooks-link-actions)
- [Foundry hosted-agent telemetry](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Azure Monitor sampling](https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-sampling)
