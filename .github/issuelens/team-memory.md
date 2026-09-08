# IssueLens team memory

## Wiki location and access

Maintain the wiki belonging to `microsoft/IssueLens`:
`https://github.com/microsoft/IssueLens/wiki`.
Its Git remote is `https://github.com/microsoft/IssueLens.wiki.git`.
Use only bundled GitHub App wiki tools; these addresses describe the supported
destination, not permission to use HTTP or shell tools. Discover the wiki's
default branch and pin a snapshot for each job. Credentials and publication
authorization belong to the host, not this policy.

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
policy statements; contradictions, broad rewrites, and deletions require review.

## Retrieval

Triage and critical scans should start with capabilities, limitations, and
known operational issues. Planning should include architecture, interfaces,
configuration, and decisions. Other agents select topics relevant to their
own work without delegating retrieval to the maintenance agent. Return page
links and snapshot/source revisions, and verify implementation-sensitive
conclusions against current source when wiki provenance is stale or missing.
