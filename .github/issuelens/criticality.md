# IssueLens Criticality Policy

Apply the built-in hot, blocking, and regression evidence thresholds. Use the
repository-specific guidance below to interpret impact; it does not replace the
built-in minimum evidence requirements.

## Core Functions

Treat these as core IssueLens functions:

- Serving both `POST /invocations` automation and `POST /responses` chat.
- Reading the requested repository with the bundled GitHub App MCP
  identity.
- Preventing credentials from entering model context, logs, or responses.
- Preserving repository scope and preventing unauthorized GitHub writes.
- Finding critical issues and returning the required valid JSON report.
- Performing explicitly requested duplicate, label, and assignment work.
- Loading and validating repository policy without bypassing invalid config.

Notification delivery is core only when the requested and configured channel is
the user's required outcome. Failure of one optional channel is not blocking
when another requested channel succeeds or the report remains available.

## High-Impact Signals

Give additional weight to concrete evidence of:

- Token, private-key, secret, or notification-endpoint disclosure.
- Cross-repository data access or a write performed without explicit user
  authorization.
- Both protocols failing to start or all requests failing authentication with
  otherwise valid credentials.
- Valid issue scans returning malformed, empty, or materially incomplete JSON.
- Label or assignment writes removing existing repository state.
- A valid `.github/issuelens.yml` making its configured capability unusable.

## Workarounds

A workaround is viable only when it preserves the same security boundary and
requested outcome. Switching from chat to invocations, bypassing GitHub App
identity, exposing a token, disabling validation, or manually performing an
unauthorized write is not a viable workaround.

## Regression Evidence

Require evidence that the behavior worked in an earlier IssueLens version,
commit, deployment, or protocol package version. A feature request for behavior
that never existed is not a regression.

Documentation-only defects, sample wording, and local setup questions are not
blocking unless they cause a supported production path to fail with no viable
workaround.