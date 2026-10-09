# Queue Team Memory Action

A composite action that validates a trusted default-branch push, uploads its
bounded identity-only source artifact, and sends one dispatch to the central
team-memory coordinator. It does not log in to Azure or invoke IssueLens.

With all coordinator target inputs omitted, the current pilot accepts
only `microsoft/IssueLens` pushes from
`.github/workflows/team-memory-post-merge.yml` and dispatches
`.github/workflows/team-memory-coordinator.yml` in that same repository.
Supplying all three target inputs enables generic dispatch to an independently
validated coordinator repository, workflow, and branch. This does not enable workflows,
create credentials, or extend repository access. The receiver must own its
trusted source/workflow allowlist, artifact provenance checks, privacy and wiki
scope, queue, and final maintenance validation.

## Usage

The [IssueLens push workflow](../../workflows/team-memory-post-merge.yml) loads
this action and its sibling `issuelens` helper from `github.workflow_sha` with
credentials not persisted, then calls `./.github/actions/queue-team-memory`.
The source workflow still owns its push trigger, opt-in gate, permissions, and
timeout.

A pinned remote action reference uses the same interface without a caller
checkout. Replace `FULL_COMMIT_SHA` with the reviewed 40-character commit SHA;
this placeholder is not a published version. This example applies only to the
IssueLens pilot's trusted source workflow:

```yaml
- name: Queue team-memory update
  uses: microsoft/IssueLens/.github/actions/queue-team-memory@FULL_COMMIT_SHA
  with:
    source-token: ${{ github.token }}
    dispatch-token: ${{ steps.dispatch-token.outputs.token }}
```

Both inputs default to `${{ github.token }}`, so the same-repository pilot
needs no token-minting step or new secret. If an explicit `dispatch-token` is
used, the caller must provide it, for example through a preceding GitHub App
token-minting step named `dispatch-token`. A separate dispatch App must not use
the hosted IssueLens App's private key. The action does not create credentials.

| Input | Purpose |
| --- | --- |
| `source-token` | Source repository read access for provenance validation, including the validation immediately before dispatch. |
| `dispatch-token` | Coordinator metadata read access and Actions write access for the single dispatch POST. An explicitly empty token fails instead of falling back. |
| `coordinator-repository` | Coordinator `owner/repository`. Defaults to empty; supply all three target inputs together or omit all of them. |
| `coordinator-workflow` | YAML workflow basename, such as `team-memory-coordinator.yml`, not a path, URL, workflow ID, or display name. Defaults to empty. |
| `coordinator-ref` | Coordinator branch, independent of the source default branch. Defaults to empty. |

The IssueLens source job retains `contents: read` and `actions: write`
permissions. A source repository's `GITHUB_TOKEN` does not expand repository access
when its permissions change; cross-repository callers must provide a
token authorized for the central repository. The coordinator will separately
need Actions read access to allowed source repositories.
For generic target checks, a fine-grained/App dispatch token needs Contents
read access (branch lookup) as well as Actions write access (workflow lookup
and dispatch) on the coordinator repository. The action does not install an App
or modify those permissions.

### Generic / Cross-Repository Dispatch

This example targets Java Pack's coordinator on `main`; the source workflow
must still run from its own current default branch, which may be `develop`.
It belongs in `.github/workflows/team-memory-post-merge.yml`. The example is
an interface illustration, not workflow enablement or a credential setup step:

```yaml
- name: Queue team-memory update
  uses: microsoft/IssueLens/.github/actions/queue-team-memory@FULL_COMMIT_SHA
  with:
    source-token: ${{ github.token }}
    dispatch-token: ${{ steps.dispatch-token.outputs.token }}
    coordinator-repository: microsoft/vscode-java-pack
    coordinator-workflow: team-memory-coordinator.yml
    coordinator-ref: main
```

Target values use a deliberately simple ASCII schema: an owner (1-39
alphanumeric/hyphen characters, starting and ending alphanumeric) and repository
(1-100 alphanumeric/dot/underscore/hyphen characters, starting alphanumeric);
a workflow basename starting alphanumeric with at most 100 characters before
`.yml` or `.yaml`; and a branch up to 255 characters whose slash-separated
components start alphanumeric and contain only alphanumeric/dot/underscore/hyphen.
`..`, empty branch components, components ending in `.` or `.lock`, and
`refs/`-prefixed branch names are rejected. Paths, URLs, query strings, and
revision expressions are not accepted.

Target syntax is checked before artifact preparation and again before dispatch.
Immediately before the POST, authenticated reads with `dispatch-token` verify
the target repository identity, exact active workflow path, and branch identity
with a full nonzero commit SHA. Source validation independently uses
`source-token`; neither credential is substituted for the other. The branch
need not be the source branch or the target default branch; the receiver must
enforce its own allowed coordinator ref.

Generic dispatch sends exactly four string inputs: `source_repository`,
`source_run_id`, `source_run_attempt`, and `source_artifact_id`. The source
repository comes only from `GITHUB_REPOSITORY`, verified against the event and
authenticated canonical repository identity; there is no caller-selectable
source repository input. The three IDs come from the original source run,
attempt, and upload output. An explicit target, including a same-repository
target, uses this four-input contract. Omitting all target inputs retains the
IssueLens pilot's existing three-ID payload and source default-branch ref;
its coordinator and the separate `issuelens` invocation action are unchanged.

### Source Artifact Contract

Both modes upload `issuelens-team-memory-source-${GITHUB_RUN_ATTEMPT}`, retained
for seven days, from
`${RUNNER_TEMP}/issuelens-team-memory-source/source-event.json`. The JSON is
bounded to 64 KiB and exactly two top-level keys, `metadata` and `event`:

| Object | Exact fields |
| --- | --- |
| `metadata` | `repository`, `repository_id`, `base_ref`, `event_name`, `event_action`, `actor_login`, `triggering_actor`, `workflow_ref`, `workflow_sha`, `run_id`, `run_attempt` |
| `event` | `repository: {id, full_name}`, `ref`, `before`, `after`, `created`, `deleted`, `forced`, `commits: [{id}, ...]`, `head_commit: {id}` |

Repository/run/attempt IDs are integers; the repository is the canonical source
name. `event_name` and `event_action` are `push`. `base_ref` is the source
default branch, `ref` is `refs/heads/` plus that branch, and `workflow_ref`
identifies that source's `.github/workflows/team-memory-post-merge.yml` at the
same branch. Actors are the workflow's `GITHUB_ACTOR` and
`GITHUB_TRIGGERING_ACTOR`. `workflow_sha`, `GITHUB_SHA`, `after`, and
`head_commit.id` must be the same full nonzero SHA. `before` must be a distinct
full nonzero SHA. The three push flags are exactly `false`. All 1-1000 supplied
commit IDs are retained in order, must be unique full nonzero SHAs, include
`after`, and exclude `before`. Empty/oversized inventories are rejected.
No target, token, body, message, or code fields are added.

The dispatcher preserves the complete supplied push inventory; the receiver
must authenticate the source run/attempt and workflow path/identity, check the
artifact's immutable ID, exact name, origin, SHA-256 digest, size, retention and
expiry before downloading, and reject digest mismatches. It must then revalidate
the exact snapshot and the authoritative complete fast-forward range before
discovery/invocation. A nonempty but truncated event inventory is not proof of
completeness. Upload or dispatch acceptance alone is not trusted source evidence
or write authorization.

## Execution and Outcomes

The action runs preparation, pinned artifact upload, and one bounded dispatch
in order. Failure stops the sequence; there is no retry or agent invocation.
Every GitHub HTTP operation is bounded, rejects redirects, and uses a 30-second
transport timeout. Target authentication failures stop before the dispatch POST;
an artifact may already exist at that point.
The artifact contains only source identities, not source code, commit messages,
issue/PR bodies, agent responses, or credentials, and expires after seven days.
The shared standard-library helper is resolved relative to `github.action_path`
from the sibling `issuelens` directory in the same pinned action bundle, not
from the caller's checkout.

The `source-artifact-id` output identifies the uploaded artifact for diagnostics.
An artifact may have been uploaded even when dispatch fails. Its presence proves
neither dispatch acceptance nor maintenance completion. A transport error may
leave dispatch outcome unknown; inspect coordinator runs before retrying.

The composite action executes in the caller repository. The actual queue lives
in the selected coordinator workflow, not in the action. The unchanged
[IssueLens coordinator](../../workflows/team-memory-coordinator.yml) allows
one active run and at most 100 pending runs in
`issuelens-team-memory-wiki-microsoft-IssueLens`, with `queue: max` and no
in-progress cancellation. Only that coordinator invokes IssueLens and validates
the final maintenance receipt. This does not serialize chat or external direct
invocations, guarantee delivery, or prove a timed-out hosted invocation stopped.
