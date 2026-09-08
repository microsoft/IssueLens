# Project team-memory policy

Use this example as the Markdown policy referenced by
`instructions.team_memory.path` in the project's `.github/issuelens.yml`.
Adapt the topics and page names to the project. This policy does not enable
publication, grant access, or supply credentials.

## Wiki location and access

Use this target repository's own GitHub wiki through bundled GitHub App tools.
The host derives its `.wiki.git` remote from the explicit `owner/repository`.
Document the project's canonical wiki URL here for maintainers, but do not
select a different repository, Git host, credential, or arbitrary transport.
Discover the existing default branch rather than assuming main or master.
If a different wiki destination is required, report it as unsupported until
the host explicitly supports and authorizes that mapping.

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
policy require review. No knowledge impact means no wiki change.

## Retrieval

Readers select relevant topics through the shared read-only team-memory skill.
Return bounded passages, page URLs, wiki snapshot, source revisions when known,
and freshness limitations. Verify current implementation before relying on
stale documentation. Readers must not propose or publish changes.
