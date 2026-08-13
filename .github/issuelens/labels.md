# IssueLens Labeling Policy

Only add labels that currently exist in this repository. Preserve all existing
labels and never create, remove, or rename labels.

## Issue Type Labels

- `bug`: Reproducible behavior where IssueLens does not meet its documented
  runtime, security, protocol, or triage contract.
- `feature-request`: A request for a new capability or an intentional expansion
  of the current IssueLens scope.
- `enhancement`: An improvement to existing IssueLens behavior or capabilities.
- `documentation`: A correction or improvement limited to documentation,
  examples, schemas, or setup guidance.
- `question`: A usage, configuration, design, or support question without
  evidence of a product defect.
- `duplicate`: The issue reports the same defect or request as an existing
  issue and satisfies the duplicate-detection evidence threshold.
- `needs more info`: The report lacks enough detail to identify the failing
  protocol, operation, trigger, environment, or expected behavior.

Choose the single best primary type label among `bug`, `feature-request`,
`enhancement`, `documentation`, and `question`. Add `needs more info` when the
missing evidence prevents reliable triage. Add `duplicate` only when a specific
canonical issue has been identified with sufficient technical evidence.

Do not infer a priority or component label unless that label exists and its
repository description supports the classification. Security-sensitive impact
influences criticality and priority reasoning but does not authorize inventing
or applying a security label.