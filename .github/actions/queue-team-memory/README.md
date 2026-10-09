# Queue Team Memory Action

A composite action that validates a trusted default-branch push, uploads its
bounded identity-only source artifact, and sends one dispatch to the central
team-memory coordinator. It does not log in to Azure or invoke IssueLens.

The current pilot accepts only `microsoft/IssueLens` pushes from
`.github/workflows/team-memory-post-merge.yml` and dispatches
`.github/workflows/team-memory-coordinator.yml` in that same repository.
Packaging the dispatch sequence does not enable Java tooling repositories or
cross-repository sources; that rollout still needs a validated source allowlist,
cross-repository source/artifact access, and its own wiki-specific queue.

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
| `dispatch-token` | Actions write access to the coordinator repository, used only for the dispatch POST. An explicitly empty token fails instead of falling back. |

The IssueLens source job retains `contents: read` and `actions: write`
permissions. A source repository's `GITHUB_TOKEN` does not expand repository access
when its permissions change; future cross-repository callers must provide a
token authorized for the central repository. The coordinator will separately
need Actions read access to allowed source repositories.

## Execution and Outcomes

The action runs preparation, pinned artifact upload, and one bounded dispatch
in order. Failure stops the sequence; there is no retry or agent invocation.
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
in the [central coordinator workflow](../../workflows/team-memory-coordinator.yml),
not in the action: one active run and at most 100 pending runs in
`issuelens-team-memory-wiki-microsoft-IssueLens`, with `queue: max` and no
in-progress cancellation. Only that coordinator invokes IssueLens and validates
the final maintenance receipt. This does not serialize chat or external direct
invocations, guarantee delivery, or prove a timed-out hosted invocation stopped.
