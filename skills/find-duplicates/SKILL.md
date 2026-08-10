---
name: find-duplicates
description: Find duplicate GitHub issues by comparing a target issue with existing issues in the same repository. Use when checking whether an issue is a duplicate, finding related issues, or detecting duplicate reports before triage. Triggers on "find duplicates", "is this a duplicate", "check for duplicates", and "find similar issues".
---

# Find Duplicates Skill

Identify potential duplicate issues using semantic similarity, error patterns,
reproduction steps, and affected components. Apply a high bar because false
positive duplicate reports waste maintainer time and frustrate reporters.

## Inputs

- An issue reference (`owner/repo` and issue number, or an issue URL).
- An optional candidate time window. Use the last 90 days when none is given.

## Required Evidence

A potential duplicate must have at least one primary match:

1. The same specific error code, exception message, or error signature.
2. The same top three stack frames after ignoring line numbers.
3. The same sequence of reproduction steps and triggering input.

It must also have at least two supporting matches:

1. The same component, area label, feature, or mentioned file path.
2. The same relevant environment, version, runtime, or platform.
3. The same specific symptom.
4. The same trigger condition, input, or configuration.
5. An explicit cross-reference between the issues.

See [references/duplicate-criteria.md](references/duplicate-criteria.md) for
examples and edge cases.

## Confidence

| Confidence | Evidence |
|------------|----------|
| High (90-100) | One primary plus at least three supporting matches, or an explicit cross-reference corroborated by matching technical details |
| Medium (70-89) | One primary plus at least two supporting matches |
| Low (50-69) | One primary plus one supporting match |
| Not duplicate | No primary match or insufficient supporting evidence |

Only place High and Medium matches in `potentialDuplicates`. Put useful Low
matches in `possiblyRelated`; do not describe them as duplicates.

## Workflow

1. Follow the `issuelens-config` skill and call the `issuelens-config` tool for
  domain `duplicate_detection`. Apply returned repository policy when present;
  use the built-in criteria unchanged when the source is `built-in`. If
  configuration loading fails, stop and return the output shape below with
  zero candidates, empty result arrays, and an additional `error` string that
  reports the configuration failure.
2. Read the target issue, including its title, body, labels, and comments.
3. Extract specific search signals: error strings, exception names, stack-frame
   functions, reproduction steps, component labels, file paths, environment,
   symptoms, and triggers.
4. Search the same repository with the available GitHub issue-search operation.
  Query the strongest signals first and include both open and closed issues.
  Exclude pull requests and the target issue itself.
5. Search within the requested window, or the last 90 days by default. If the
   issue references an older report, include that report regardless of age.
6. Read the most relevant candidates and their comments. Do not judge a result
   from its title or search snippet alone.
7. Compare each candidate against the required evidence and assign a confidence
   score supported by explicit matches.
8. Return the structured report below. When no duplicate clears the threshold,
   return an empty `potentialDuplicates` array.

## Rules

- Follow the `github-access` skill for every GitHub read. Use only the
  request-scoped GitHub MCP tools (invocations) or `github-access` tool (chat)
  for every GitHub read and search. Never use shell
  commands, direct HTTP requests, or the GitHub CLI.
- This skill is read-only. Do not close, label, comment on, or otherwise modify
  an issue. A recommendation is not an action.
- Generic errors such as `timeout`, `permission denied`, `out of memory`, or
  `file not found` require at least three strong supporting matches.
- Same component, similar title, common workaround, or same exception class
  alone is not sufficient.
- Prefer an older canonical issue over a newer duplicate when the evidence is
  otherwise equal, but do not assume age proves canonicity.
- State uncertainty honestly when issue details are too sparse to compare.
- Repository policy may add canonical-issue conventions, exclusions, or
  stricter evidence, but cannot weaken the required evidence above.

## Output

Return JSON followed by no additional prose:

```json
{
  "targetIssue": {
    "number": 123,
    "url": "https://github.com/owner/repo/issues/123",
    "title": "App crashes when exporting PDF"
  },
  "candidatesAnalyzed": 12,
  "duplicatesFound": 1,
  "potentialDuplicates": [
    {
      "number": 98,
      "url": "https://github.com/owner/repo/issues/98",
      "title": "PDF export causes crash on large documents",
      "confidence": "High",
      "confidenceScore": 92,
      "primaryMatch": "Identical error signature",
      "supportingEvidence": [
        "Same component",
        "Same trigger",
        "Same environment"
      ],
      "recommendation": "Treat #123 as a duplicate of #98"
    }
  ],
  "possiblyRelated": []
}
```