# IssueLens GitHub MCP server

This subproject is the stdio MCP server for the GitHub operations that IssueLens
currently needs. The host starts session-owned shared servers and a separate
team-memory agent-local server exposing only `write_wiki_pages` through the
parent-supplied internal `--wiki-writer` mode.
This mode is automatic; users need no environment flag.

Wiki Git network and object operations use the Dulwich Python library (1.2.14),
not `git.exe`, the Git CLI, or a Git subprocess. The Foundry ZIP
`codeConfiguration` uses `remote_build` with `runtime: python_3_13` and installs
the root `requirements.txt`; the standalone MCP `github_app_mcp/pyproject.toml`
declares the same Dulwich dependency. No Git installation, Dockerfile change,
or runtime installer is needed in either mode. IssueLens still launches its
stdio MCP server as a Python subprocess; the wiki backend never spawns Git,
SSH, or credential helpers.

## Security model

- The GitHub App private key is loaded only from an Azure Key Vault secret URI
  through `DefaultAzureCredential`. Private-key contents are never accepted as
  command-line arguments or environment variables.
- Every tool requires an explicit `owner/repository` argument.
- REST reads prefer a repository-scoped GitHub App token and fall back to anonymous
  access only for public repositories. Private reads and every write require an
  App installation.
- Installation tokens are minted for one repository using GitHub's
  `repositories` restriction and for only the permission required by the tool.
  The cache key includes the repository and permission set.
- Issue write tools are absent from MCP discovery unless the trusted host sets
  `GITHUB_MCP_ENABLE_WRITES=true`. The GitHub client enforces the same gate.
- Only the team-memory agent-local server exposes wiki writes. Shared
  reader/triage servers do not expose `write_wiki_pages`. Tool availability and
  repository policy do not authorize a maintenance job to write.
- The server exposes fixed GitHub REST routes. It has no generic HTTP, REST, or
  GraphQL tool. Wiki operations use the bundled `.wiki` Git backend for only
  the validated wiki destination's `.wiki.git`, with no caller-selected remote.
- Search qualifiers cannot change repository, organization, or user scope.
  Repository paths, pagination, file sizes, comments, names, and tool results
  are bounded.
- The stdio server writes no application output to stdout outside MCP framing.

Install the App on every target repository and any private related repository
IssueLens is authorized to access. Public related repositories can be searched
through bounded anonymous reads without an installation.

## Tools

The shared server registers these REST read tools. The permission shown is used when an App
installation is available; public repositories can fall back to anonymous
access:

| Tool | Preferred App permission |
|---|---|
| `get_repository` | Metadata: read |
| `list_issues` | Issues: read |
| `get_issue` | Issues: read |
| `list_issue_comments` | Issues: read |
| `get_issue_comment` | Issues: read |
| `list_issue_reactions` | Issues: read |
| `search_issues` | Issues: read |
| `list_labels` | Issues: read |
| `get_file` | Contents: read |
| `get_pull_request` | Pull requests: read |
| `list_pull_request_files` | Pull requests: read |
| `list_pull_request_commits` | Pull requests: read |
| `list_pull_request_reviews` | Pull requests: read |
| `list_pull_request_review_comments` | Pull requests: read |
| `get_commit` | Contents: read |
| `list_change_files` | Contents: read; separately Pull requests: read when initially resolving a PR |
| `read_diff_chunk` | Contents: read |
| `read_file_range` | Contents: read |
| `compare_commits` | Contents: read |
| `list_repository_tree` | Contents: read |
| `search_repository_content` | Contents: read |
| `list_merged_pull_requests` | Pull requests: read |

With a supplied `ref`, `search_repository_content` scans an immutable snapshot:
at most 64 regular files, 256 KiB of eligible content (64 KiB per file), and
66 content API requests for a full SHA (one lightweight `/git/commits` object,
one tree, and up to 64 blobs). Branch/tag inputs resolve through fixed
`/git/ref/heads`, `/git/ref/tags`, and at most eight `/git/tags` reads before the
immutable commit-object read: at most ten additional requests, still inside
the same scan deadline. `HEAD` first reads the repository's default branch.
Search never downloads the patch-bearing `/commits/{ref}` response.
Abbreviated SHA inputs remain supported using a bounded, metadata-only
one-item commit list after branch/tag lookup misses, then the full Git commit
object; this is not the patch-bearing commit-detail endpoint. App
authentication lookup/minting is performed once per scan; initial authentication
overhead is additional and is not counted in those 66 content requests. One
HTTP client is reused for all content requests in a scan, never shared across
scans, and closed on success, error, cancellation, or deadline expiry. A fixed
60-second overall scan time budget includes authentication, commit/tree/blob
response-body reads, and local result construction; it never resets per request.
Exceeding it fails explicitly without returning partial results or falling back
to indexed search. Host cancellation propagates after cleanup. A missing-installation
result is retained only for that scan; no negative authentication cache survives
the scan. Anonymous fallback remains read-only and never authorizes writes.
Without `ref`, search uses GitHub's indexed default-branch code search with its
existing single-request 30-second HTTP timeout, not the scan budget.

### Large immutable changes

The three change readers are read-only IssueLens extensions, not renamed
upstream tools. The legacy `get_commit`, PR-file listing, comparison, and
`get_file` contracts remain unchanged. Their **128 KiB HTTP / 100,000-byte JSON
limits are not raised**. Smaller REST pages cannot fix one enormous patch, and
REST PR-files/comparison inventories have 3,000/300-file ceilings.

1. `list_change_files(repository, pull_number=None, commit_sha=None,
   base_sha=None, head_sha=None, cursor=None, per_page=50)` requires exactly one
   target: PR number, full commit SHA, or an explicit full base/head pair.
   Commits compare against their **first parent**, including merge commits; a
   root commit has a null base. PRs pin both tips and compute their true merge
   base through a bounded commit-graph walk. Multiple merge bases, inaccessible
   fork objects, and exhausted graph budgets fail rather than substituting the
   base-branch tip or widening repository access.

   The result is `{repository, snapshot, files, next_cursor, complete}`.
   `snapshot` contains `{snapshot_id, base_sha, head_sha, mode}` and, for PRs,
   `pull_number`. Modes are `pull_request`, `commit`, and `compare`.
   `snapshot_id` hashes the case-insensitive repository, resolved base/head, and
   format version, **not the caller's mode**. Each file contains
   `{path, status, old_blob_sha, new_blob_sha, old_mode, new_mode}`. Status is
   `added`, `removed`, `modified`, or `type_changed`; renames may be explicit
   remove/add pairs. For mode `160000`, the object SHA is a gitlink's commit,
   not a text blob.

   Directory/file replacements use remove/add entries for the file at the
   transition path and the affected descendants. Diff reads use the same
   identities: the directory side of a replacement is absent, and a descendant
   cannot exist below a non-directory ancestor. Links are never followed;
   direct directory-only diff targets remain explicitly unsupported.

   Inventories walk immutable, nonrecursive Git trees, skipping equal subtree
   hashes. They do not download patches and are not limited to 3,000 files.
   A truncated tree fails explicitly. Pages contain at most `per_page` entries
   (1–100), reduced further if needed to fit 32 KiB. `complete` means inventory
   exhaustion, not analysis completion. Repeat the **same original selectors**
   with `next_cursor`; a PR cursor retains its original tips and merge base
   instead of following a subsequently moved PR. Compare returned snapshot IDs
   before combining evidence.

2. `read_diff_chunk(repository, base_sha, head_sha, path, cursor=None,
   max_bytes=24576)` takes that pinned comparison and one path. `base_sha` is
   required but nullable for a root. It returns `{repository, snapshot_id,
   path, chunk_id, content, old_start, old_end, new_start, new_end,
   representation, status, next_cursor, complete, old_blob_sha, new_blob_sha}`.
   Nontext results also contain `reason`.

   `max_bytes` (1,024–32,768) bounds the **final serialized JSON result**,
   including escaping, line ranges, IDs, and continuation metadata; the MCP
   JSON text is also tested against this ceiling. Protocol framing is separate.
   Use approximately 4,096 bytes for analysis batches. A very long path or
   repository name can require more than the minimum to fit metadata.
   The backend does not send a complete diff to the model.

   `representation="unified"` chunks concatenate exactly into one unified diff.
   Chunks can split inside a hunk or line; line ranges are inclusive, may repeat
   for continued line fragments, and are `0/0` for an absent side or headers.
   Fine matching exceeding its work allowance instead emits
   `replacement-old` followed by `replacement-new`: **lossless raw old/new
   source blocks**, not a minimal patch. Concatenate each side's fragments
   separately to reconstruct the original contents. No newline is invented or
   dropped. Only the last file fragment has a null `next_cursor`.

   `status` is `text`, `binary`, or `unsupported`. Binary data, non-UTF-8 or
   unsupported blob encodings, symlinks, submodules, directories, and blobs
   above the size limit have explicit notices/reasons, never "unchanged" text.
   Links are not followed. A finished nontext notice has `complete=true`,
   meaning the disposition was returned, **not that its contents were analyzed**.

3. `read_file_range(repository, sha, path, start_line=1, end_line=120,
   cursor=None, max_bytes=24576)` reads surrounding pinned UTF-8 source using
   the same blob reader. `sha` must be a full commit SHA; the inclusive request
   may span at most 10,000 lines. It returns `{repository, sha, path, chunk_id,
   content, start_line, end_line, next_cursor, complete, status}` plus `reason`
   for nontext content. Returned positions describe actual source fragments.
   Empty files and ranges beyond EOF return empty text with explicit `0/0`
   positions. Giant lines continue losslessly across pages. Repeat the
   **original** requested line range with each cursor, not the returned
   fragment's positions. `complete` refers to that requested range, not all
   source context. No HTTP Range support is assumed.

All SHAs in these three tools are full, 40-character GitHub SHA-1 identities.
Blob sizes, base64, and the actual Git blob hash are checked before text is
used. Chunk IDs and cursors bind repository, snapshot, path/blob identity,
format version, and exact character offsets. Cursors use a deterministic
integrity checksum and are portable across workers; they are **not signatures,
credentials, or grants of access**. Each call re-establishes operation-scoped
App access. Anonymous fallback verifies current public repository metadata
before it can reuse cached source from an earlier authorized call. No tokens
occur in cursor payloads, cache keys, or results.

The separate change reader streams only fixed repository `/git/trees/{sha}`
and `/git/blobs/{sha}` routes with a larger ingress budget. Commit/PR metadata
retains a 128 KiB HTTP cap. Redirects, arbitrary URLs/queries, ambient
credentials, Git/shell subprocesses, and worktree checkouts are not used.
Budgets fail explicitly, not with a misleading partial inventory:

| Boundary / constant in `changes.py` | Limit |
|---|---|
| `MAX_CHANGE_BLOB_BYTES` | 4 MiB decoded per blob |
| `MAX_CHANGE_OBJECT_HTTP_BYTES` | 8 MiB per tree/blob JSON response |
| `MAX_CHANGE_OPERATION_BYTES` / `MAX_CHANGE_OPERATION_REQUESTS` | 64 MiB / 1,024 requests per tool call |
| `MAX_CHANGE_SESSION_BYTES` / `MAX_CHANGE_SESSION_REQUESTS` | 512 MiB / 16,384 requests per client lifetime |
| `MAX_CHANGE_SECONDS` | 60 seconds per call, including lock wait/authentication; cooperative CPU checks |
| `MAX_CHANGE_FILES` | 20,000 changed paths |
| `MAX_CHANGE_TREE_ENTRIES` / `MAX_CHANGE_TREES` / `MAX_CHANGE_DEPTH` | 100,000 entries / 1,024 distinct trees / 64 path components |
| `MAX_CHANGE_GRAPH_COMMITS` / `MAX_CHANGE_PARENTS` | 512 commit-graph nodes / 64 parents per commit |
| `MAX_CHANGE_CACHE_BYTES` / `MAX_CHANGE_CACHE_ENTRIES` | 64 MiB accounted memory / 512 LRU entries |
| `CHANGE_CACHE_TTL_SECONDS` | 300 seconds, also bounded by the owning client/session lifetime |
| `MAX_DIFF_MATCH_LINES` / `MAX_DIFF_MATCH_CELLS` | 2,000 lines per side / 250,000 line-pair cells |
| `MAX_DIFF_MATCH_INPUT_BYTES` / `MAX_DIFF_MATCH_WORK` | 256 KiB combined matching input / 16 MiB character-by-line work estimate |
| `MIN_CHANGE_PAGE_BYTES` / `DEFAULT_CHANGE_PAGE_BYTES` / `MAX_CHANGE_RESULT_BYTES` | 1,024 / 24,576 / 32,768 serialized bytes |
| `MAX_CHANGE_CURSOR_BYTES` / `MAX_FILE_RANGE_LINES` | 2,048 cursor characters / 10,000 requested source lines |

Authentication lookup/minting overhead is additional to content request/byte
counts, but remains inside the call's cooperative time budget. Checks and
cancellation bound network work; they are **not hard CPU deadlines**.
Immutable objects, manifests, and computed diff representations share the
session-owned LRU cache, avoiding repeated blob downloads and comparisons for
every 4 KiB page. Eviction or a new worker may require refetching, never a
different snapshot. The cache contains no credentials and uses no files or
persistent storage; only counters and immutable evidence live with the client.

Wiki tools always take `repository` as the **source project**, even when its
memory is stored in another repository's wiki. The shared package policy parser
validates the source's structured `instructions.team_memory` with a required
policy `path` and optional `wiki_repository`. The latter is a GitHub parent
repository identifier, not a wiki UI name or Git URL. The config tool returns
resolved `wiki_repository` alongside policy `content`; Markdown guides only
organization and topics, never target, arbitrary Git URL, token, or shell settings.

Every wiki read/write tool independently re-reads and validates that same mapping
and resolves credentials and transport to the destination. An omitted field,
config, or domain defaults to the source project's own wiki. A misconfigured or
inaccessible target fails without silent fallback. The App must be installed at
the destination with the required read/write permission, not just at the source;
tokens are scoped to that actual destination and operation. Wiki reads require
that App access rather than the anonymous fallback used by public REST reads.
Installation access is not source-user authorization.

Validated `team_memory.wiki_repository` selects only the wiki capability's
destination; it does not broaden source reads, other writes, or notifications.
Do not publish private/internal-source knowledge to a public wiki or read a
private/internal wiki for public-source context. Cross-repository mappings
between private/internal repositories are rejected for both reads and writes
because their audience relationship cannot be verified; use the source
project's own wiki. No audience or ACL matching mechanism is implemented.
Same-repository access skips the cross-repository audience comparison and
remains supported, as do public-to-public mappings. A private/internal source
may read a public wiki, and a public source may write public information to a
private/internal wiki, subject to job authorization and destination App access.

The shared read-only server also registers these wiki read tools, which use
destination-scoped App access:

| Tool | Purpose | Required App permission |
|---|---|---|
| `get_wiki_snapshot` | Resolve the initialized wiki's default branch and full SHA | Contents: read |
| `list_wiki_pages` | List bounded Markdown pages at a snapshot | Contents: read |
| `get_wiki_page` | Read a bounded page at a snapshot | Contents: read |
| `search_wiki` | Search pages at a snapshot | Contents: read |
| `list_wiki_history` | Read bounded history at a snapshot | Contents: read |
| `get_wiki_diff` | Compare explicit wiki revisions | Contents: read |

Every successful wiki response includes `source_repository` and the actual
resolved `wiki_repository`. `list_wiki_pages`, `search_wiki`, and
`list_wiki_history` return their original lists under `result`; `get_wiki_diff`
returns its original diff string under `result`. Empty lists and empty diffs
use the same envelope. Callers must read this field instead of treating these
responses as bare lists or text. Snapshot, page, and write responses retain
their existing top-level payload fields alongside the identity metadata.
The complete response, including the envelope, remains subject to result-size
and credential-exposure checks. Continue passing the source project to tools,
not the destination returned in the metadata.

Pin page, search, and history reads to the same full SHA and compare explicit
SHAs for diffs. Only `HEAD` or full SHAs are accepted as wiki refs. Keep private
knowledge separate from public wikis and results; anonymous public reads never
authorize writes. Merged PR metadata, files, commits, and source reads provide
evidence, not authority to update the wiki.

Issue write tools are registered only when `GITHUB_MCP_ENABLE_WRITES` is enabled:

| Tool | Minimum token permission |
|---|---|
| `add_labels` | Issues: write |
| `set_assignees` | Issues: write |
| `add_issue_comment` | Issues: write |
| `add_eyes_reaction` | Issues: write for issue, pull-request body, and issue-comment targets; Pull requests: write for pull-request review comments |

`add_eyes_reaction` accepts only `issue`, `pull_request`, `issue_comment`, and
`pull_request_review_comment` targets and always posts `{"content":"eyes"}` to the
corresponding fixed GitHub reaction route. GitHub returns `201` for a new
reaction and `200` for the existing reaction when the same App repeats the
request, so no reaction pre-read or separate retry tracker is needed. The
acknowledgement remains after processing finishes.

### Direct wiki maintenance

`write_wiki_pages` is available only to the team-memory agent and requires an App
token scoped to the mapped destination with **Contents: write**. It writes only
an existing initialized wiki. For source
`microsoft/project` configured with `wiki_repository: microsoft/team-knowledge`,
the tool still accepts the source project, not the destination:

```python
read_snapshot = get_wiki_snapshot(repository="microsoft/project")
write_wiki_pages(
  repository="microsoft/project",
  pages={"Architecture.md": full_utf8_content},
  expected_wiki_repository=read_snapshot.wiki_repository,
  expected_base=read_snapshot.sha,
  message=short_summary,
)
```

Both expected values are required and come from the same snapshot read.
`expected_wiki_repository` is a precondition, never a destination override.
It must be a valid GitHub `owner/repository` identifier and match the freshly
resolved policy destination case-insensitively before any destination metadata,
token lookup, or wiki access. Policy still selects the actual destination and
scoped App credentials. A mismatch is rejected even if the SHA is unchanged.

`read_snapshot.sha` is the full SHA returned by the snapshot read. Supply complete UTF-8
contents for changed `.md` pages, not patches: at most 20 pages, 64 KiB per page,
and 256 KiB total. Create/update only; deletion and rename are unsupported. The
pages cite evidence and include the full source commit SHA, not an abbreviation,
where relevant; the short commit message also includes that full SHA. No generic URL,
force, token, credential, or approval arguments are accepted.

Typed validation, byte and cooperative time budgets, and redirect denial bound
the internal HTTPS `x-access-token` transport. Only SHA-1 Git repositories
(GitHub's current format) are supported; SHA-256 repositories are rejected.
Operations use bounded temporary PACK storage and in-memory Git objects, without
a full worktree checkout, hooks, filters, or Git config discovery. Timeouts are
cooperative across socket, library, and DNS operations, not a hard CPU deadline.
Diffs report binary-change notices, not binary patches; unchanged assets are
preserved byte-for-byte and page deletion is unsupported.

The backend in [src/issuelens_github_mcp/wiki.py](src/issuelens_github_mcp/wiki.py)
owns snapshots and an atomic Git commit, persisting knowledge and history with
a non-force update against `expected_base`. No separate host publisher,
database, or proposal/approval persistence is involved. If no knowledge changes,
do not write. On a stale-base conflict, re-read and regenerate against the new
snapshot; never blindly retry. If a response was lost, compare desired contents
with current pages first. On a destination mismatch, read a fresh snapshot and
re-establish destination, authorization, and evidence; never automatically
overwrite or reuse edits against another wiki, or merely replace the expected
repository to retry. Report the new SHA and status only when confirmed by
the tool result, not merely because a write was attempted.

Only an explicit current request or parent handoff authorizing a wiki update
for that target authorizes maintenance, regardless of its origin. Sensitive, conflicting,
destructive, or unsupported requests need ordinary human interaction. The
read-only skill never writes or delegates ordinary reads to the writer.
Git provides knowledge, history, and conflict detection, not a durable job queue,
reconciliation service, external scheduler, or guaranteed exactly-once delivery.
The optional [post-merge workflow](../.github/workflows/team-memory-post-merge.yml)
uses the [shared IssueLens action](../.github/actions/issuelens/README.md) with
`request-type: team-memory` to submit validated merged-PR jobs using Azure OIDC.
The same action supports issue-loop and direct tasks without imposing the wiki
result schema on their answers. It never accesses the wiki
directly or holds App credentials. Its task constraints and result format are
supplied in the request, not assumed by the agent or the tools. The agent revalidates source evidence and
uses these same tools, privacy guards, and paired write preconditions. See the
[setup and retry guidance](../README.md#post-merge-team-memory-automation).

## Configuration

| Environment variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | Yes | Numeric GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_SECRET_URI` | Yes | Azure Key Vault secret URI containing the App PEM |
| `GITHUB_MCP_ENABLE_WRITES` | No | `false` by default; enables issue write tools only for an authorized session |

Wiki destination configuration belongs only in the source project's structured
`instructions.team_memory.wiki_repository`; there is no per-repository App
environment configuration. The parent supplies `--wiki-writer` automatically
only for the team-memory agent-local MCP server with
`tools: ["write_wiki_pages"]`. Users need no environment flag. The existing
`GITHUB_MCP_ENABLE_WRITES` gate remains for triage issue writes and does not
enable wiki writes or require a new user setting. Explicit job authorization
and destination App installation access are both still required.

The process identity needs Azure Key Vault secret `get` permission. The GitHub
App needs Metadata read, Contents read, Issues read/write, and Pull requests
read/write for the issue toolset; wiki maintenance additionally needs Contents
write at the destination. GitHub narrows each minted token below the App's maximum
permissions. All private-key, installation, and token caches live
only in the stdio process and are discarded when that session-owned process
exits.

## Local verification

Install the subproject into the repository virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\github_app_mcp
```

Run all isolated tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
  -s .\github_app_mcp\tests -p "test_*.py" -v
```

For a real local server, authenticate to Azure using a credential supported by
`DefaultAzureCredential`, set the required variables above, and run:

```powershell
.\.venv\Scripts\issuelens-github-mcp.exe
```

To check whether the configured App is installed for one or more repositories,
run the diagnostic script from the repository root:

```powershell
.\.venv\Scripts\python.exe .\github_app_mcp\scripts\check_installations.py `
  microsoft/IssueLens microsoft/another-repository
```

The script loads the root `.env` file when `python-dotenv` is installed, reads
the App private key from Key Vault using `DefaultAzureCredential`, and prints
only the installation status and installation ID. Exit code `0` means every
target is installed, `1` means at least one target is not installed, and `2`
means configuration, authentication, or GitHub API validation failed.

Equivalent Copilot SDK stdio configuration shape:

```python
{
    "type": "stdio",
    "command": r"C:\path\to\.venv\Scripts\issuelens-github-mcp.exe",
    "env": {
        "GITHUB_APP_ID": "123456",
        "GITHUB_APP_PRIVATE_KEY_SECRET_URI": (
            "https://vault-name.vault.azure.net/secrets/github-app-key"
        ),
        "GITHUB_MCP_ENABLE_WRITES": "false",
    },
    "tools": ["*"],
}
```

Do not add private-key contents to this configuration.
