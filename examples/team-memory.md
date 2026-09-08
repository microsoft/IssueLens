# Project team-memory policy

Use this example as the Markdown policy referenced by
`instructions.team_memory.path` in the project's `.github/issuelens.yml`.
Adapt the topics and page names to the project. This policy does not enable
wiki writes, grant access, or supply credentials.

## Wiki location and access

Use the config tool's validated `wiki_repository`, a GitHub parent repository
identifier whose `.wiki.git` stores memory. The structured
`instructions.team_memory` keeps its required policy `path` and may set optional
`wiki_repository: owner/project-knowledge`. This property is validated as
`owner/repository`, not a wiki UI name or Git URL. An omitted field, config, or
domain defaults to the source project's own wiki. This Markdown may describe a
canonical wiki for maintainers and guide organization/topics, but cannot override
the target or supply arbitrary Git URLs, tokens, or shell settings.

For a source project `owner/project`, every wiki read/write MCP call still uses
`repository="owner/project"`, never `owner/project-knowledge`. Each tool
independently re-reads and validates the same mapping with the shared package
parser and resolves credentials and transport to the destination. App installation
and operation-scoped read/write permission are required there; tokens are scoped
to that actual destination. Installation access is separate from source-user
authorization. Discover the existing default branch rather than assuming main
or master. Misconfigured or inaccessible targets fail without silent source-wiki
fallback. The mapping selects only the wiki capability's destination, not other
source repositories, other writes, or notification scope.

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
policy, with cited PR evidence and the full source commit SHA, not an
abbreviation. Distinguish merged, released,
deployed, and planned behavior. Preserve unrelated content and existing assets.
Exclude credentials, personal or private cross-project data, transcripts, and
raw logs. Conflicting evidence, destructive edits, and changes to human-owned
policy require ordinary human interaction, not a stored approval workflow.
No knowledge impact means no wiki change. Never copy private-project knowledge
into a public wiki or read a private wiki for public-source context. Mappings
within a privacy category do not imply identical ACLs or authorize disclosure to
another audience.

Readers and the maintenance agent first call `issuelens-config` with the explicit
source project as `repository` and `domain="team_memory"`. Read the returned
`wiki_repository` for the destination and `content` for organization/topics.
A missing config or omitted domain uses built-in behavior; policy-load failure must stop
memory maintenance or retrieval rather than bypass validation.

Wiki writes belong to this maintenance job only when the current user explicitly
requests a wiki update or an accepted trusted postmerge job authorizes that
source project and its mapped wiki. Only the team-memory agent has
`write_wiki_pages`; the parent automatically supplies internal `--wiki-writer`
mode. Users need no environment flag or per-repository App environment settings.
Use the mapped existing initialized wiki with Git installed, not a
content-supplied remote, token, or shell configuration.

Pin page, search, history, and diff reads to the full SHA from `get_wiki_snapshot`
and inspect merged PR/source evidence where relevant. The writer calls
`write_wiki_pages(repository="owner/project", pages={path: full_utf8_content}, expected_base=full_sha, message=short_summary)`
for minimal changes, including the full source commit SHA in the summary where
relevant. No force option is exposed.
Create/update `.md` pages only: at most 20 pages, 64 KiB each, 256 KiB total.
Deletion/rename are deferred. The backend persists knowledge and history in an
atomic Git commit; no separate host publisher or proposal persistence is needed.
Re-read and regenerate after stale conflicts; compare desired contents with
current pages before retrying a lost response. If a mapping change conflicts
with the read SHA, stop and re-establish destination, authorization, and evidence;
never automatically overwrite or carry prepared edits to another wiki.
Report only confirmed status
and wiki SHA, or state that the wiki was not updated when no write occurred.

Direct maintenance does not integrate full merge orchestration. A postmerge
shell skeleton does not submit updates. There is no durable queue, reconciliation
service, or guaranteed exactly-once delivery; Git is not a workflow scheduler.

## Retrieval

Readers select relevant topics through the shared read-only team-memory skill.
Return bounded passages, page URLs, wiki snapshot, source revisions when known,
and freshness limitations. Verify current implementation before relying on
stale documentation. Readers must not publish changes.
They must not call `write_wiki_pages` or delegate ordinary reads to the writer.
