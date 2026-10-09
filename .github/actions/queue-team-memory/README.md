# Standalone Workflow Dispatch Action

A generic, self-contained transport that validates a target repository,
workflow, and branch, then sends one bounded GitHub `workflow_dispatch` with
caller-supplied inputs. Its name/path is retained for existing team-memory
callers, but it does not prepare source events, upload/download artifacts,
discover PRs, invoke IssueLens, or interpret wiki policy or job authorization.
Its only script, `dispatch.py`, lives in this action directory; there are no
sibling action imports, checkout dependencies, or external Python packages.

## Inputs

| Input | Purpose / default |
| --- | --- |
| `dispatch-token` | Target Contents read and Actions write access; defaults to `${{ github.token }}`. An explicitly empty token fails without fallback. |
| `coordinator-repository` | Target `owner/repository`; defaults to `${{ github.repository }}`. |
| `coordinator-workflow` | Required YAML workflow basename, such as `team-memory-coordinator.yml`, not a filesystem path, workflow ID, URL, or display name. |
| `coordinator-ref` | Target branch; defaults to `${{ github.ref_name }}`. Set it explicitly when the central branch differs from the caller's branch. |
| `workflow-inputs` | JSON object with at most ten string-valued inputs; defaults to `{}`. No source or job fields are synthesized. |

Target values use a simple ASCII schema: owner 1-39 alphanumeric/hyphen
characters, starting and ending alphanumeric; repository 1-100
alphanumeric/dot/underscore/hyphen characters, starting alphanumeric; workflow
basename starting alphanumeric with at most 100 characters before `.yml` or
`.yaml`; branch up to 255 characters with slash-separated components starting
alphanumeric and containing only alphanumeric/dot/underscore/hyphen.
`..`, empty branch components, components ending in `.` or `.lock`, and
`refs/`-prefixed branch names are rejected. Inputs use names starting with a
letter or underscore followed by up to 99 alphanumeric/underscore/hyphen
characters. Duplicate JSON keys, nonstring values, invalid JSON, and raw input
or encoded dispatch payloads over 64 KiB fail before network access. Do not
include credentials or sensitive bodies in dispatch inputs.

The token must be caller-provided for cross-repository dispatch. A source
repository's `GITHUB_TOKEN` does not expand repository access when permissions
change. The action does not create credentials, install an App, change
permissions, or grant the receiving job authorization.

## Team-Memory Usage

The source workflow owns request-specific evidence preparation and artifact
upload **before** this action. In IssueLens,
[team-memory-post-merge.yml](../../workflows/team-memory-post-merge.yml) loads
trusted code at `github.workflow_sha` with credentials not persisted, calls the
request-owned `issuelens_action.py prepare-source` helper with a source read
token, and uploads its sanitized identity-only push artifact with a pinned
uploader. The artifact retains original `before`, `after`, and all commit IDs;
the source run API alone cannot recover the original push range. The
[invocation action](../issuelens/README.md#issuelens-coordinator-pilot) owns the
artifact schema and the receiving validation/download contract.

A pinned remote dispatch action needs no caller checkout. Replace
`FULL_COMMIT_SHA` with a reviewed full commit SHA; this is not a published version.
After preparation/upload, the IssueLens pilot supplies its unchanged three-ID
payload to its same-repository coordinator:

```yaml
- name: Queue team-memory request
  uses: microsoft/IssueLens/.github/actions/queue-team-memory@FULL_COMMIT_SHA
  with:
    coordinator-workflow: team-memory-coordinator.yml
    workflow-inputs: '{"source_run_id":"${{ github.run_id }}", "source_run_attempt":"${{ github.run_attempt }}", "source_artifact_id":"${{ steps.artifact.outputs.artifact-id }}"}'
```

For a source using `develop` and a central coordinator using `main`, set the
target ref independently and supply the receiving coordinator's own payload:

```yaml
- name: Queue team-memory request
  uses: microsoft/IssueLens/.github/actions/queue-team-memory@FULL_COMMIT_SHA
  with:
    dispatch-token: ${{ steps.dispatch-token.outputs.token }}
    coordinator-repository: microsoft/vscode-java-pack
    coordinator-workflow: team-memory-coordinator.yml
    coordinator-ref: main
    workflow-inputs: '{"source_repository":"${{ github.repository }}", "source_run_id":"${{ github.run_id }}", "source_run_attempt":"${{ github.run_attempt }}", "source_artifact_id":"${{ steps.artifact.outputs.artifact-id }}"}'
```

The second example is a later consumer migration contract, not enablement or
a change to Java Pack. The caller authenticates/prepares its source separately
from this target-only action. Receivers own source/workflow allowlists,
run/attempt/artifact provenance and digest checks, privacy/wiki scope, the
central queue, and final maintenance validation. Arbitrary dispatch input
claims are not trusted provenance or write authorization.

## Transport and Outcomes

Before a write, authenticated GETs independently verify the target repository
identity, exact active workflow path, and selected branch with a full nonzero
SHA. All HTTP operations reject redirects, use a 30-second timeout, and bound
metadata responses to 64 KiB. One POST is attempted, with no automatic retry.
Errors are static/sanitized and never disclose response bodies, input values,
or credentials.

Acknowledgement means only that GitHub accepted the dispatch, not that a
coordinator ran, was admitted to a queue, invoked the agent, or completed a job.
An ambiguous POST failure reports an unknown outcome; inspect target runs
before retrying. There is no success-shaped job receipt or artifact output.
The action executes in the caller repository; the actual concurrency queue
remains in the selected central workflow, not in the action.

**Compatibility change:** unlike the prior bundled dispatcher, this action
requires `coordinator-workflow` and caller-owned `workflow-inputs`, no longer
accepts `source-token`, and does not prepare/upload a source artifact or infer
`source_repository`/run IDs. Consumers pinned to the older immutable revision
continue unchanged until deliberately migrated.
