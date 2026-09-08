# Project team-memory policy

Use this example as the Markdown policy referenced by
`instructions.team_memory.path` in the project's `.github/issuelens.yml`.
Adapt the topics and page names to the project. This policy does not enable
wiki writes, grant access, or supply credentials.

## Wiki location and access

Use this target repository's own GitHub wiki through bundled GitHub App tools.
The bundled MCP `.wiki` backend derives its `.wiki.git` remote from the explicit
`owner/repository`.
Document the project's canonical wiki URL here for maintainers, but do not
select a different repository, Git host, credential, or arbitrary transport.
Discover the existing default branch rather than assuming main or master.
If a different wiki destination is required, report it as unsupported until
the supported capability changes; do not substitute a remote or repository.

## Structure and priorities

Preserve existing navigation and human-maintained pages. Maintain a short topic
index and prefer focused updates over new pages for every PR. Suggested topics:

- Architecture: components, interfaces, dependencies, and design rationale.
- Features: behavior, configuration, compatibility, and limitations.
- Engineering: onboarding, local setup, testing, and contribution guidance.
- Operations: release, deployment, rollback, telemetry, and troubleshooting.
- Processes: ownership, support practices, and explicit team decisions.

Prioritize topics affected by the current change. Do not create empty pages
for every suggested topic or invent processes not established by the team.

## Content and maintenance

Include durable knowledge supported by source/tests or explicit maintainer
policy, with PR and immutable-source references. Distinguish merged, released,
deployed, and planned behavior. Preserve unrelated content and existing assets.
Exclude credentials, personal or private cross-project data, transcripts, and
raw logs. Conflicting evidence, destructive edits, and changes to human-owned
policy require ordinary human interaction, not a stored approval workflow.
No knowledge impact means no wiki change. Never copy private-project knowledge
into a public wiki.

Readers and the maintenance agent first call `issuelens-config` with the explicit
repository and `domain="team_memory"` and apply the returned `content`. A missing
config or omitted domain uses built-in behavior; policy-load failure must stop
memory maintenance or retrieval rather than bypass validation.

Wiki writes belong to this maintenance job only when the current user explicitly
requests a wiki update or an accepted trusted postmerge job authorizes that
target. The host's `ISSUELENS_WIKI_WRITE_REPOSITORIES` allowlist enables capability,
not permission from this policy. Use an existing initialized wiki with Git
installed; no arbitrary remote, credential, or other repository is permitted.

Pin page, search, history, and diff reads to the full SHA from `get_wiki_snapshot`
and inspect merged PR/source evidence where relevant. The writer calls
`write_wiki_pages(repository, pages={path: full_utf8_content}, expected_base=full_sha, message=short_summary)`
for minimal changes, including the PR/source SHA in the summary where relevant.
Create/update `.md` pages only: at most 20 pages, 64 KiB each, 256 KiB total.
Deletion/rename are deferred. The backend persists knowledge and history in an
atomic Git commit; no separate host publisher or proposal persistence is needed.
Re-read and regenerate after stale conflicts; compare desired contents with
current pages before retrying a lost response. Report only confirmed status
and wiki SHA, or state that the wiki was not updated when no write occurred.

Direct maintenance does not integrate full merge orchestration. A postmerge
shell skeleton does not submit updates. There is no durable queue, reconciliation
service, or guaranteed exactly-once delivery; Git is not a workflow scheduler.

## Retrieval

Readers select relevant topics through the shared read-only team-memory skill.
Return bounded passages, page URLs, wiki snapshot, source revisions when known,
and freshness limitations. Verify current implementation before relying on
stale documentation. Readers must not propose or publish changes.
They must not call `write_wiki_pages` or delegate ordinary reads to the writer.
