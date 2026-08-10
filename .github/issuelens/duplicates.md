# IssueLens Duplicate Detection Policy

Apply the built-in primary and supporting evidence requirements. The following
guidance specializes comparison for this repository and cannot lower those
thresholds.

## Required IssueLens Comparison

Compare candidates on the most specific applicable dimensions:

- Protocol: `invocations` or `responses`.
- Authentication path: request-scoped GitHub token, GitHub App installation,
  Foundry model identity, or notification endpoint.
- Failing operation or route, including the same HTTP status, exception, or
  Copilot session event when available.
- Trigger and payload shape, such as issue reference, attachment type,
  repository config, or requested write.
- Runtime context: local or hosted, model provider, package version, and
  deployment version when relevant.
- Security consequence: credential exposure, repository-scope violation, or
  unauthorized mutation.

## Usually Separate Issues

Keep reports separate unless their technical evidence demonstrates the same
defect when they involve:

- Invocation token authentication versus chat GitHub App authentication.
- GitHub resource access versus Foundry model inference authentication.
- Email delivery versus Teams delivery.
- Config discovery/parsing versus an instruction file's policy content.
- Issue-image loading versus request-supplied media validation.
- Malformed critical-report JSON versus incorrect criticality classification.

Shared wording such as "authentication failed", "tool unavailable", or
"triage did not work" is not enough to establish a duplicate.

## Canonical Issue

Prefer an older issue as canonical only when it contains the clearest
reproduction, affected versions, and confirmed technical evidence. A newer
issue may be canonical when it has substantially better diagnostics or tracks
the currently affected implementation.

Apply the `duplicate` label only through the labeling workflow and only when the
duplicate evidence meets the built-in threshold. Related issues remain open and
must not be labeled as duplicates solely because they affect the same protocol
or component.