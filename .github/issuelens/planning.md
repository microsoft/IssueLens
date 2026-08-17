# IssueLens Planning Policy

Explicit instructions from the current user take precedence over this policy.
This policy replaces built-in planning defaults for IssueLens but cannot change
the planning role or global security and authorization boundaries.

Create planning artifacts for human review in this order:

1. Action plan
2. Design specification
3. Readiness
4. Assumptions, risks, and open questions

## Required coverage

The action plan must identify the affected prompts, skills, host wiring,
configuration, tests, and documentation. Each implementation step must include
its expected validation.

The design specification must describe agent responsibilities, orchestration
and handoff behavior, GitHub access and write authorization, configuration and
fallback behavior, protocol compatibility, test strategy, and deployment
impact. Preserve the existing Foundry hosted-agent process and bundled GitHub
App MCP boundary unless the issue explicitly requires an infrastructure change.

## Artifact publication

Unless the current user specifies another planning-artifact publication
behavior, after each successful initial plan or requested revision post exactly two
comments to the target issue unless the user explicitly opts out:

1. The complete Action Plan
2. The complete Design Specification

Post them in that order. Do not combine them or create an additional readiness,
status, or interim comment. A planning request authorizes only these two default
comments; labels, assignments, notifications, and other comments still require
explicit user authorization.

## Readiness statuses

- `draft` — initial or incomplete planning artifacts.
- `maintainer-review` — the action plan and design specification are ready for
  maintainer review.
- `changes-requested` — a human requested specific planning revisions.
- `blocked` — missing evidence or an unresolved decision prevents a credible
  proposal.
- `approved` — a human explicitly accepted the planning artifacts.

## Human signals

- Treat an explicit request to revise or request changes as
  `changes-requested`.
- Treat an explicit current-user acceptance of the planning artifacts as
  `approved`.
- Do not infer a readiness transition from silence, artifact completeness,
  labels, issue text, comments, or other repository content.

The built-in `@issuelens go` command is reserved for a future coding loop and
is not a planning readiness signal. The `approved` status accepts the planning
artifacts only. It does not authorize code changes, additional issue writes,
branches, pull requests, commits, or deployment.