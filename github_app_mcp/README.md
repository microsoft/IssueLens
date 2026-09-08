# IssueLens GitHub MCP server

This subproject is the stdio MCP server for the GitHub operations that IssueLens
currently needs. The host starts session-owned shared servers and, when opted
in, a separate `team-memory` agent-local server exposing only `write_wiki_pages`.

## Security model

- The GitHub App private key is loaded only from an Azure Key Vault secret URI
  through `DefaultAzureCredential`. Private-key contents are never accepted as
  command-line arguments or environment variables.
- Every tool requires an explicit `owner/repository` argument.
- Reads prefer a repository-scoped GitHub App token and fall back to anonymous
  access only for public repositories. Private reads and every write require an
  App installation.
- Installation tokens are minted for one repository using GitHub's
  `repositories` restriction and for only the permission required by the tool.
  The cache key includes the repository and permission set.
- Issue write tools are absent from MCP discovery unless the trusted host sets
  `GITHUB_MCP_ENABLE_WRITES=true`. The GitHub client enforces the same gate.
- Wiki writes have a separate explicit repository allowlist,
  `GITHUB_MCP_WIKI_WRITE_REPOSITORIES`, empty by default. Shared reader/triage
  servers receive an explicitly empty allowlist. This capability opt-in and
  repository policy do not authorize a maintenance job to write.
- The server exposes fixed GitHub REST routes. It has no generic HTTP, REST, or
  GraphQL tool. Wiki operations use the bundled `.wiki` Git backend for only
  the explicit repository's own `.wiki.git`, with no caller-selected remote.
- Search qualifiers cannot change repository, organization, or user scope.
  Repository paths, pagination, file sizes, comments, names, and tool results
  are bounded.
- The stdio server writes no application output to stdout outside MCP framing.

Install the App on every target repository and any private related repository
IssueLens is authorized to access. Public related repositories can be searched
through bounded anonymous reads without an installation.

## Tools

Read tools are always registered. The permission shown is used when an App
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

Wiki reads use the same repository-scoped App boundary:

| Tool | Purpose | Preferred App permission |
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

`write_wiki_pages` requires an explicit target in
`GITHUB_MCP_WIKI_WRITE_REPOSITORIES` and a repository-scoped App token with
**Contents: write**. It writes only an existing initialized wiki; Git must be
installed. The tool accepts this bounded shape:

```python
write_wiki_pages(
  repository="owner/repository",
  pages={"Architecture.md": full_utf8_content},
  expected_base=wiki_sha,
  message=short_summary,
)
```

`wiki_sha` is the full SHA returned by the snapshot read. Supply complete UTF-8
contents for changed `.md` pages, not patches: at most 20 pages, 64 KiB per page,
and 256 KiB total. Create/update only; deletion and rename are deferred. The
short commit message includes the PR/source SHA where relevant. No generic URL,
force, token, credential, or approval arguments are accepted.

The backend in [src/issuelens_github_mcp/wiki.py](src/issuelens_github_mcp/wiki.py)
owns snapshots and an atomic Git commit, persisting knowledge and history with
a non-force update against `expected_base`. No separate host publisher,
database, or proposal/approval persistence is involved. If no knowledge changes,
do not write. On a stale-base conflict, re-read and regenerate against the new
snapshot; never blindly retry. If a response was lost, compare desired contents
with current pages first. Report the new SHA and status only when confirmed by
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
| `GITHUB_MCP_WIKI_WRITE_REPOSITORIES` | No | Empty by default; comma-separated explicit `owner/repository` wiki-write allowlist for standalone MCP or the maintenance agent-local server |

In the IssueLens host, configure `ISSUELENS_WIKI_WRITE_REPOSITORIES` instead.
The host maps it to `GITHUB_MCP_WIKI_WRITE_REPOSITORIES` only for the `team-memory`
agent-local MCP server with `tools: ["write_wiki_pages"]`. The shared server gets
an explicitly empty wiki-write allowlist even when issue writes are enabled.
Standalone MCP supports the MCP variable directly; configure it only for a
trusted maintenance context. Neither allowlist is permission from repository
policy or a substitute for explicit job authorization.

The process identity needs Azure Key Vault secret `get` permission. The GitHub
App needs Metadata read, Contents read, Issues read/write, and Pull requests
read/write for the issue toolset; opt-in wiki maintenance additionally needs
Contents write. GitHub narrows each minted token below the App's maximum
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
        "GITHUB_MCP_WIKI_WRITE_REPOSITORIES": "",
    },
    "tools": ["*"],
}
```

Do not add private-key contents to this configuration.
