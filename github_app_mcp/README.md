# IssueLens GitHub MCP server

This subproject is the stdio MCP server for the GitHub operations that IssueLens
currently needs. `main.py` starts one server process for each Copilot session.

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
- Write tools are absent from MCP discovery unless the trusted host sets
  `GITHUB_MCP_ENABLE_WRITES=true`. The GitHub client enforces the same gate.
- The server exposes fixed GitHub REST routes. It has no generic HTTP, REST, or
  GraphQL tool.
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

Write tools are registered only when writes are enabled:

| Tool | Minimum token permission |
|---|---|
| `add_labels` | Issues: write |
| `set_assignees` | Issues: write |
| `add_issue_comment` | Issues: write |
| `add_eyes_reaction` | Issues: write for issue targets; Pull requests: write for pull-request targets |

`add_eyes_reaction` accepts only `issue`, `pull_request`, `issue_comment`, and
`pull_request_comment` targets and always posts `{"content":"eyes"}` to the
corresponding fixed GitHub reaction route. GitHub returns `201` for a new
reaction and `200` for the existing reaction when the same App repeats the
request, so no reaction pre-read or separate retry tracker is needed. The
acknowledgement remains after processing finishes.

## Configuration

| Environment variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | Yes | Numeric GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_SECRET_URI` | Yes | Azure Key Vault secret URI containing the App PEM |
| `GITHUB_MCP_ENABLE_WRITES` | No | `false` by default; set `true` only for an authorized session |

The process identity needs Azure Key Vault secret `get` permission. The GitHub
App needs Metadata read, Contents read, Issues read/write, and Pull requests
read/write for the complete toolset. GitHub narrows each minted token below the
App's maximum permissions. All private-key, installation, and token caches live
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
