# IssueLens GitHub MCP server

This subproject is the stdio MCP server for the GitHub operations that IssueLens
currently needs. The host starts session-owned shared servers and a separate
team-memory agent-local server exposing only `write_wiki_pages` through the
parent-supplied internal `--wiki-writer` mode.
This mode is automatic; users need no environment flag.

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

Wiki reads use destination-scoped App access:

| Tool | Purpose | Required App permission |
|---|---|---|
| `get_wiki_snapshot` | Resolve the initialized wiki's default branch and full SHA | Contents: read |
| `list_wiki_pages` | List bounded Markdown pages at a snapshot | Contents: read |
| `get_wiki_page` | Read a bounded page at a snapshot | Contents: read |
| `search_wiki` | Search pages at a snapshot | Contents: read |
| `list_wiki_history` | Read bounded history at a snapshot | Contents: read |
| `get_wiki_diff` | Compare explicit wiki revisions | Contents: read |

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
an existing initialized wiki; Git must be installed. For source
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
and 256 KiB total. Create/update only; deletion and rename are deferred. The
pages cite evidence and include the full source commit SHA, not an abbreviation,
where relevant; the short commit message also includes that full SHA. No generic URL,
force, token, credential, or approval arguments are accepted.

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

Only an explicit current-user wiki-update request or an accepted trusted
postmerge job for that target authorizes maintenance. Sensitive, conflicting,
destructive, or unsupported requests need ordinary human interaction. The
read-only skill never writes or delegates ordinary reads to the writer.
Git provides knowledge, history, and conflict detection, not a durable job queue,
reconciliation service, external scheduler, or guaranteed exactly-once delivery.
Full merge orchestration is separate; a postmerge shell skeleton is not a
functional automatic-update integration.

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
