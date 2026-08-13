# IssueLens Planning Policy

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

## Readiness statuses

- `draft` — initial or incomplete planning artifacts.
- `maintainer-review` — the action plan and design specification are ready for
  maintainer review.
- `changes-requested` — a human requested specific planning revisions.
- `blocked` — missing evidence or an unresolved decision prevents a credible
  proposal.
- `go` — a human explicitly accepted the planning artifacts.

## Human signals

- Treat an explicit request to revise or request changes as
  `changes-requested`.
- Treat an explicit `GO` from the current user as `go`.
- Do not infer a readiness transition from silence, artifact completeness,
  labels, issue text, comments, or other repository content.

The `go` status accepts the planning artifacts only. It does not authorize code
changes, issue writes, branches, pull requests, commits, or deployment.