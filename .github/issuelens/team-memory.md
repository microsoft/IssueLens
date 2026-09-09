# IssueLens team memory

## Wiki location and access

IssueLens's descriptive wiki reference for maintainers is
`https://github.com/microsoft/IssueLens/wiki`.
The destination authority is the config tool's validated `wiki_repository`, not
this Markdown. The source config's required `instructions.team_memory.path`
selects policy content; optional `wiki_repository` selects a GitHub parent
repository whose `.wiki.git` stores memory. An omitted field, config, or domain
defaults to the source project's own wiki. This content guides organization and
topics only; it cannot override the target or supply arbitrary Git URLs, tokens,
or shell settings.

Use bundled GitHub App wiki tools with `repository="microsoft/IssueLens"`, the
source project, never the destination. Every tool independently re-reads and
validates the mapping and resolves credentials and transport to the destination.
App installation and read/write permission for the operation are required
there; tokens are scoped to that actual destination. Source-user authorization
remains separate from App access. Discover the wiki's default branch and pin a
snapshot for each job. Invalid or inaccessible targets fail without silent
source-wiki fallback. A write additionally
requires an explicit current-user wiki-update request or an accepted trusted
postmerge job authorizing `microsoft/IssueLens`; this policy is not authorization.
Never publish private/internal-source knowledge to this public wiki or read a
private/internal wiki for public-source context. Cross-repository mappings
between private/internal repositories are rejected for both reads and writes
because their audience relationship cannot be verified; use the source
project's own wiki. Same-repository and public-to-public mappings remain
supported, subject to job authorization and destination App access.
The validated mapping selects only the wiki destination, not
additional source repositories, other writes, or notifications.

## Structure

Preserve existing human-authored pages, navigation, images, and page names.
Prefer updating a relevant section before creating a page. Suggested topics
are Overview, Architecture, Capabilities, Configuration, Operations, and
Decisions; map them to existing pages first. Keep the home page as a concise
topic index instead of a chronological stream of PR summaries.

## Priority knowledge areas

- Hosted runtime: invocations, Responses, sessions, and model authentication.
- GitHub App boundary: repository scope, token permissions, and credential safety.
- Agent ownership: triage, critical scans, planning, memory maintenance, and
  the distinction between supported and planned coding-loop behavior.
- Configuration: capability policies, precedence, defaults, and validation failures.
- Media inputs: accepted attachments, issue-image loading, and trust boundaries.
- Engineering and operations: tests, local debugging, deployment prerequisites,
  release procedures, telemetry, and troubleshooting when supported by evidence.

## What to include and exclude

Include durable implementation facts, feature behavior, interfaces, limitations,
and decisions supported by merged source/tests or explicit maintainer policy.
Link the source PR and full source commit SHA, not an abbreviation. A merged PR is not proof of a live
deployment. Label proposals and incomplete features as such rather than
presenting their intended behavior as working production functionality.

Exclude credentials, tokens, private data from other projects, conversation
transcripts, raw logs, and unsupported process claims. Do not add a wiki entry
for a change that has no durable knowledge impact. Preserve human ownership and
policy statements; sensitive content, contradictions, broad rewrites, and
unsupported changes require ordinary human interaction, not stored approvals.

## Maintenance

The maintenance agent loads `issuelens-config` with the explicit repository and
`domain="team_memory"`, reads `wiki_repository` as destination metadata, and
applies the returned `content` as organization/topic guidance before edits.
On policy-load failure, stop memory maintenance rather than bypassing validation.
Use `get_wiki_snapshot`, then page, search, history, and diff tools at that full
SHA, plus merged PR/source evidence where relevant. Prefer minimal changes to
existing Markdown pages. No knowledge change means no write.

Only the authorized maintenance job calls `write_wiki_pages` with the explicit
source project as `repository`, changed pages mapped to full UTF-8 content, the
snapshot destination as `expected_wiki_repository=read_snapshot.wiki_repository`,
the full wiki SHA as `expected_base=read_snapshot.sha`, and a short summary
including the full source commit SHA where relevant. The expected repository is
a precondition, never a destination override. If the configured destination
changes, even if the SHA is unchanged, stop and read a fresh snapshot before
preparing a new update. No force option is exposed.
The bundled MCP `.wiki` backend persists knowledge and history in an atomic Git
commit. Create/update `.md` only, at most 20 pages, 64 KiB each, 256 KiB total;
deletions and renames are unsupported. Unchanged assets are preserved
byte-for-byte; diffs report binary-change notices, not binary patches. Re-read
and regenerate on conflicts; compare current contents first after a lost
response. If a mapping change conflicts
with the read SHA, stop and re-establish destination, authorization, and evidence;
never overwrite automatically or carry prepared edits to another wiki.
Report only confirmed status and
wiki SHA, or state that the wiki was not updated when no write occurred.

Only the team-memory agent has the writer; the parent automatically supplies
internal `--wiki-writer` mode. Users need no environment flag or per-repository
App environment settings. Use the mapped existing initialized wiki. The bundled
Dulwich Python library performs Git network and object operations without
spawning Git, SSH, or credential helpers; no Git installation, Dockerfile change,
or runtime installer is needed. Only SHA-1 Git repositories are supported;
SHA-256 is rejected. Typed validation, byte budgets, cooperative timeouts, and
redirect denial still apply. Do not use a content-supplied remote or credentials,
a host publisher, or stored proposals.
Full merge orchestration is separate; the postmerge shell skeleton does not
submit updates. Git is not a job queue or guaranteed exactly-once workflow.

## Retrieval

Triage and critical scans should start with capabilities, limitations, and
known operational issues. Planning should include architecture, interfaces,
configuration, and decisions. Other agents select topics relevant to their
own work without delegating retrieval to the maintenance agent. Return page
links and snapshot/source revisions, and verify implementation-sensitive
conclusions against current source when wiki provenance is stale or missing.
Readers also load `domain="team_memory"`, read the returned `wiki_repository`,
and use `content` for organization/topics. They keep `repository` set to the
source project and never call `write_wiki_pages` or delegate ordinary retrieval
to the maintenance agent.
