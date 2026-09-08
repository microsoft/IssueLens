# IssueLens team memory

## Wiki location and access

Maintain the wiki belonging to `microsoft/IssueLens`:
`https://github.com/microsoft/IssueLens/wiki`.
Its Git remote is `https://github.com/microsoft/IssueLens.wiki.git`.
Use only bundled GitHub App wiki tools; these addresses describe the supported
destination, not permission to use HTTP or shell tools. Discover the wiki's
default branch and pin a snapshot for each job. Credentials and publication
capability belong to the trusted host, not this policy. A write additionally
requires an explicit current-user wiki-update request or an accepted trusted
postmerge job authorizing `microsoft/IssueLens`; this policy is not authorization.
Keep private-project knowledge out of this public wiki.

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
Link the source PR and immutable revision. A merged PR is not proof of a live
deployment. Label proposals and incomplete features as such rather than
presenting their intended behavior as working production functionality.

Exclude credentials, tokens, private data from other projects, conversation
transcripts, raw logs, and unsupported process claims. Do not add a wiki entry
for a change that has no durable knowledge impact. Preserve human ownership and
policy statements; sensitive content, contradictions, broad rewrites, and
unsupported changes require ordinary human interaction, not stored approvals.

## Maintenance

The maintenance agent loads `issuelens-config` with the explicit repository and
`domain="team_memory"` and applies the returned `content` before preparing edits.
On policy-load failure, stop memory maintenance rather than bypassing validation.
Use `get_wiki_snapshot`, then page, search, history, and diff tools at that full
SHA, plus merged PR/source evidence where relevant. Prefer minimal changes to
existing Markdown pages. No knowledge change means no write.

Only the authorized maintenance job calls `write_wiki_pages` with the explicit
repository, changed pages mapped to full UTF-8 content, the full wiki SHA as
`expected_base`, and a short summary including the PR/source SHA where relevant.
The bundled MCP `.wiki` backend persists knowledge and history in an atomic Git
commit. Create/update `.md` only, at most 20 pages, 64 KiB each, 256 KiB total;
deletions and renames are deferred. Re-read and regenerate on conflicts; compare
current contents first after a lost response. Report only confirmed status and
wiki SHA, or state that the wiki was not updated when no write occurred.

The host allowlist is a capability opt-in, not permission from this policy.
Use only the existing initialized wiki, with Git installed. Do not use another
remote, credentials supplied by content, a host publisher, or stored proposals.
Full merge orchestration is separate; the postmerge shell skeleton does not
submit updates. Git is not a job queue or guaranteed exactly-once workflow.

## Retrieval

Triage and critical scans should start with capabilities, limitations, and
known operational issues. Planning should include architecture, interfaces,
configuration, and decisions. Other agents select topics relevant to their
own work without delegating retrieval to the maintenance agent. Return page
links and snapshot/source revisions, and verify implementation-sensitive
conclusions against current source when wiki provenance is stale or missing.
Readers also load `domain="team_memory"` and use the returned `content`, but do
not write or delegate ordinary retrieval to the maintenance agent.
