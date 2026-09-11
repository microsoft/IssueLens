# IssueLens Action

A reusable composite action for issue-loop, team-memory, and explicit tasks.
It prepares and validates the request, authenticates to an existing IssueLens
agent with Azure OIDC, and invokes it through a shared bounded HTTP/SSE client.
It is a client of the agent, not part of the hosted agent runtime.

The action runs three steps: prepare the request, log in with the pinned
`azure/login` action, and submit the request. The standard-library Python helper
owns metadata validation, the caller's task text, bounded HTTP/SSE processing,
and result validation. No inline Python, pip installation, container build, or
GitHub App private key is required.

## Request Types

| `request-type` | Preparation | Result |
| --- | --- | --- |
| `issue-loop` | Accept issue opened/reopened, human issue-comment created/edited, or manual issue dispatch. Preserve workflow-owned event metadata; exclude issue/comment bodies and skip PR/bot comments before login. | A completed root agent response, without a required JSON schema. The orchestrator chooses triage, planning, or no action. |
| `team-memory` | Accept a merged PR into the default branch or a manual merged-PR request. Re-read authoritative repository and PR metadata. | Require the existing structured maintenance result matching source/PR/merge and verified wiki identity. |
| `task` | Require explicit non-empty `input`, bounded to 64 KiB UTF-8. No issue-loop event metadata or maintainer-command authority is synthesized. | A completed root agent response in the requested format. |

All request types validate the caller repository identity and that the caller
workflow runs from its current default branch before Azure login. Unknown request
types, malformed inputs, and an `input` supplied to an event adapter fail closed.
Event adapters prepare context, not a role assignment: the agent owns routing,
command validation, and task-specific write authorization. In particular there
is no separate `planning` request type.

## Use From Another Repository

Once the action files are committed and available in `microsoft/IssueLens`,
replace `FULL_COMMIT_SHA` below with the full 40-character SHA of that reviewed
commit. This is a placeholder, not a published version. A Marketplace listing,
separate repository, or package upload is unnecessary; a release tag is optional.
Pin consumers to an immutable SHA, not `main` or a moving tag.

### Team Memory

```yaml
name: Update team memory
on:
  pull_request_target:
    types: [closed]
  workflow_dispatch:
    inputs:
      pull_request_number:
        description: Merged PR to process
        required: true
        type: string
permissions: {}
concurrency:
  group: team-memory-${{ github.repository }}-${{ github.event.pull_request.number || inputs.pull_request_number || github.run_id }}
  cancel-in-progress: false
jobs:
  reconcile:
    if: >-
      vars.ISSUELENS_TEAM_MEMORY_ENABLED == 'true' &&
      ((github.event_name == 'workflow_dispatch' &&
        github.ref == format('refs/heads/{0}', github.event.repository.default_branch)) ||
       (github.event_name == 'pull_request_target' &&
        github.event.pull_request.merged == true &&
        github.event.pull_request.base.ref == github.event.repository.default_branch))
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      pull-requests: read
      id-token: write
    steps:
      - name: Maintain team memory
        id: memory
        uses: microsoft/IssueLens/.github/actions/issuelens@FULL_COMMIT_SHA
        with:
          request-type: team-memory
          pull-request-number: ${{ inputs.pull_request_number }}
          azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
          azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          azure-subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
          agent-url: ${{ secrets.ISSUELENS_AGENT_URL }}
          agent-scope: ${{ secrets.ISSUELENS_AGENT_SCOPE }}
```

External callers need no checkout: GitHub downloads the pinned action bundle.
Any caller workflow filename is supported, but the workflow must run from the
caller's current default branch. Other events and unmerged PRs are rejected.
For event adapters the source repository is derived from the caller's GitHub context, not an input
that could redirect maintenance to another source repository. PR titles, bodies,
and fork contents are not embedded in the task.

IssueLens's [issue workflow](../../workflows/issue-triage.yml) and
[team-memory workflow](../../workflows/team-memory-post-merge.yml) use the same
local action so a new action version can be reviewed and merged with its callers.
It uses pinned `actions/checkout` with `ref: github.workflow_sha`, sparse checkout
of the action directory, and `persist-credentials: false`. This checks out the
trusted workflow revision, never a PR head or merge-test ref. Do not copy that
local-action step to consumer repositories; use the remote reference above.

### Issue Loop: Triage and Planning

Use the event filters and per-issue concurrency from the
[issue workflow](../../workflows/issue-triage.yml). Its job grants
`contents: read`, `issues: read`, and `id-token: write`, and uses a 20-minute
timeout. In an external repository remove its local checkout step and change
the remaining action call to:

```yaml
- name: Process issue event
  uses: microsoft/IssueLens/.github/actions/issuelens@FULL_COMMIT_SHA
  with:
    request-type: issue-loop
    issue-number: ${{ inputs.issue_number }}
    azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
    azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
    azure-subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
    agent-url: ${{ secrets.ISSUELENS_AGENT_URL }}
    agent-scope: ${{ secrets.ISSUELENS_AGENT_SCOPE }}
```

Manual dispatch requires `issue_number` and runs only on the default branch.
The adapter re-reads that issue and skips PR-backed targets. Comment IDs, authors,
associations, and created/edited flags retain the existing issue-loop contract.
The action does not parse commands or elevate ordinary comment prose to a task.
The agent independently validates any maintainer command and chooses triage,
planning, or no action. Reporter comments still contribute evidence without
receiving maintainer-command authority.

### Direct Task

For a caller-defined task, use `request-type: task` and `input`. Keep the same
Azure inputs, `contents: read` and `id-token: write` permissions, trusted default
branch, and timeout. Choose the caller's triggers and concurrency for that task.

```yaml
- name: Request a plan
  id: plan
  uses: microsoft/IssueLens/.github/actions/issuelens@FULL_COMMIT_SHA
  with:
    request-type: task
    input: Plan microsoft/IssueLens#27 without posting comments or making other writes.
    azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
    azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
    azure-subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
    agent-url: ${{ secrets.ISSUELENS_AGENT_URL }}
    agent-scope: ${{ secrets.ISSUELENS_AGENT_SCOPE }}
```

The same input can request triage, a critical-issue scan, or explicit wiki
maintenance. It follows ordinary agent scope/authorization rules, not the
issue-loop command contract. Treat `input` as a trusted caller instruction:
never pass public issue/PR/comment text as authoritative task input. Use the
event adapter instead. Direct tasks do not manufacture event provenance even
when text claims to be a maintainer command. The action's caller repository
identity check is not authorization for any additional repositories named by a task.

## Inputs and Outputs

| Input | Required | Purpose |
| --- | --- | --- |
| `request-type` | Yes | `issue-loop`, `team-memory`, or `task`. |
| `input` | For `task` | Explicit task text, 1-64 KiB UTF-8; do not combine with event adapters. |
| `issue-number` | For manual `issue-loop` | Positive issue number; automatic events use their containing issue. |
| `github-token` | No | Defaults to `github.token`; repository read for all types, Issues read for manual issue dispatch, Pull requests read for team memory. |
| `pull-request-number` | For manual `team-memory` | Positive merged PR number. Automatic events use their own PR number. |
| `azure-client-id` | Yes | Existing Azure OIDC identity's client ID. |
| `azure-tenant-id` | Yes | Tenant used by Azure login. |
| `azure-subscription-id` | Yes | Subscription used by Azure login. |
| `agent-url` | Yes | Complete HTTPS Foundry invocations endpoint, including its API version query. |
| `agent-scope` | Yes | Entra scope for that endpoint. |
| `output-mode` | No | `hybrid` (default): live agent text plus activity; `activity`: statuses only; `quiet`: no live event display. |
| `summary-mode` | No | `full` (default): status/metrics and final answer; `status`: status/metrics only; `none`: do not write a job summary. |

| Output | Meaning |
| --- | --- |
| `status` | `skipped` for ineligible issue events; `completed` for a finished issue-loop/task invocation; `updated` or `no-change` for validated team-memory results. Unavailable on failure. |
| `skip-reason` | Fixed reason for an event skipped before Azure login. |
| `response-path` | Unique runner-local UTF-8 file containing the final root answer for `issue-loop`/`task`. |
| `wiki-repository` | Wiki identity from a successful `team-memory` result only. |
| `wiki-sha` | Full wiki commit SHA from a successful `team-memory` result only. |

**`completed` confirms transport completion, not business success or a successful
write.** A planning response may ask for clarification or report a blocked task;
an issue-loop answer may report no action. Callers can inspect `response-path`
according to their own requirements without imposing a shared result schema.
The file may contain sensitive repository data and untrusted model text: do not
execute it, interpolate it into shell commands, or publish it without review.
By default, sanitized user-facing text is streamed to the log and the final
answer is included in the job summary. The unmodified answer is never an inline
action output. Files are not uploaded as artifacts by this action.

Team-memory results additionally write validated identities to the job summary.
No access token is exposed as an action output. Each accepted preflight creates a
separate temporary request file under `RUNNER_TEMP`; it contains metadata and
task text, not authentication credentials, and follows the runner's normal
temporary-file lifetime. Do not put secrets in task text. A later invocation
does not overwrite an earlier request or response.

## Live Output and Job Summary

The default **hybrid** view combines streaming text and a compact activity
timeline. An illustrative log looks like this:

```text
[   2.1s] [IssueLens] Checking the issue and current repository state.
[   3.0s] [plan] Started.
[   3.2s] [tool] plan / github-get_file started
[   4.1s] [tool] plan / github-get_file completed (0.9s)
[   5.0s] [plan] Preparing the action plan and design specification.
[   8.4s] [warning] Retrying model request.
[  10.0s] [activity] Assistant output resumed after retry.
[  18.2s] [IssueLens] Planning is ready for review.
[  18.3s] [IssueLens] Invocation completed. Stream completion alone does not confirm requested writes.
```

Text deltas are combined into lines or chunks of at most 1,024 characters and
flushed during stream activity, approximately once per second for partial lines.
The renderer keeps a trailing buffer for known-secret masking. It also ticks on
received heartbeat lines; it cannot display new progress while a socket read is
blocked. Console output is flushed immediately, though GitHub controls when its
log viewer refreshes. This is not an interactive terminal with spinners or redraws.

Messages are tracked by agent scope and message ID. The completed-message event
does not print the streamed answer twice; a complete-message fallback supports
streams without text deltas. Known sub-agent names are displayed, and unknown
agents use `sub-agent` instead of exposing opaque identifiers. Concurrent streams
retain separate labels and buffers. Only the validated root answer is used as
the final result; sub-agent text is progress, not proof of completion.

Tools show only names, observed start/completion status, and duration, never raw
arguments or result bodies. Retry notices omit raw model errors. Generic delta
duplicates, tool-call fragments, permission and usage chatter, system prompts,
and reasoning events are excluded. A recovered retry or a failed tool does not
by itself fail the invocation; the existing stream/result checks decide success.

After completion, GitHub renders the Markdown report on the run's **Summary**
page. It includes elapsed time, observed tool/retry counts, and the final root
answer for generic requests. Team-memory gets a validated identity/SHA table and
its returned reason instead of a raw JSON object. A failed or incomplete stream
gets a failure-only report, never a partial answer presented as confirmed output.
The Summary page is not a live streaming view. The action writes no issue or PR
comments to provide this display.

### Publication Controls

**Hybrid/full publishes agent content to everyone who can read the Actions log
and summary.** The response can include private repository information even when
the caller repository is public. Choose modes appropriate for the audience:

- `output-mode: hybrid`, `summary-mode: full`: readable live text and final report.
- `output-mode: activity`, `summary-mode: status`: progress without publishing agent prose.
- `output-mode: quiet`, `summary-mode: none`: retain the runner-local final response only; normal action lifecycle messages may still appear.

These controls do not disable authentication, parsing, or result validation.
Token masking is registered with the runner before use. The renderer also
redacts known endpoint credentials/URLs, strips terminal/control sequences, and
neutralizes `::` so agent text cannot forge workflow commands or annotations.
The summary escapes raw HTML and disables Markdown images while preserving
ordinary Markdown headings, lists, code blocks, and links. Read links as
untrusted agent content, not trusted navigation generated by the action.
Control sequences are normalized incrementally before text is split into log
chunks, so a split sequence cannot bypass masking at a chunk boundary.
Masking is not a general detector for secrets or private information retrieved
from repositories; it does not replace the publication controls above.

Live output is capped at 256 KiB, with at most 256 tracked messages, tools, and
agent identities, 64 Ki characters per message, and 512 Ki characters of tracked
text. The summary is independently capped at 256 KiB, below GitHub's per-step
limit. Display truncation is reported and does not truncate the data used for
validation or the saved generic final answer. The original bounded transport
limits still apply. No raw event trace, internal reasoning, or diagnostic
artifact is uploaded by this action.
If the log writer fails, live display falls back to quiet mode. A failed summary
write is reported when possible without changing the validated invocation
outcome. Failure to save the actual response/output contract still fails the action.

## Setup and Security

- Use a GitHub-hosted Ubuntu runner with Python 3, Bash, and Azure CLI available.
  Helpers run in Python isolated mode and use no repository or agent imports.
- The caller declares permissions, opt-in, triggers, timeout, and concurrency;
  a composite action cannot grant job permissions. Preserve the example's gates.
- Configure the Azure identity's repository/event/ref-scoped OIDC federation
  and permission to invoke the agent. Issue-loop federation is not proof that
  both post-merge and manual triggers are covered. No infrastructure is created.
- Protect changes to caller workflows and review the pinned action code. Never
  execute PR-head code, untrusted scripts, or dependencies in a credentialed job.
- Configure wiki policy and destination App permissions on the agent side, then
  initialize the destination wiki and set `ISSUELENS_TEAM_MEMORY_ENABLED=true`
  in the caller repository when ready to enable automatic team memory. This
  variable is not required by the issue-loop or direct-task adapters. The action
  never writes to GitHub directly; the agent may perform explicitly authorized writes.
- Origin and output requirements are supplied through the ordinary agent
  `input`. They do not make metadata cryptographic proof or introduce a permanent
  workflow protocol into the universal agent instructions.

## Results and Recovery

The helper uses bounded GitHub reads, a masked step-local endpoint token, HTTPS
without redirects, and one write-capable POST with no automatic retries. The
response must contain a completed SSE stream and a non-empty final root
assistant message. Nested sub-agent messages and messages still requesting tools
are not accepted as final answers. Stream errors and incomplete responses fail
all request types. Team-memory additionally requires a final JSON result matching
the source repository, PR, and merge SHA; only `updated` or `no-change` with wiki
identity and full SHA succeeds. Its `needs-review` or `failed` result fails the
step. Issue-loop and task results may be plain text, Markdown, or requested JSON.

The submission socket timeout is 60 seconds, the cooperative stream budget is
15 minutes, and the stream is limited to 8 MiB total and 1 MiB per line. Keep a
20-minute job timeout as in the example. These are not hard real-time guarantees.

Different PR jobs may overlap or finish out of order. The agent retains its
current-knowledge checks and wiki compare-and-swap safeguards. After an ambiguous
failure, inspect the target's current state before retrying. For team memory,
inspect the mapped wiki/history before retrying the same merged PR.
Content comparison avoids unnecessary writes but does not provide a durable
queue, guaranteed delivery, or exactly-once execution.

Local tests exercise the imported helper and action/caller wiring with mocked
services. They do not establish live OIDC federation, hosted writer dispatch,
or wiki publication. See the [project setup guide](../../../README.md#post-merge-team-memory-automation).
