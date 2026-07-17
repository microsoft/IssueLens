---
name: triage
description: Triage GitHub issues to identify critical (hot, blocking, regression) issues for one or more repositories and produce a structured JSON summary report. Use when asked to triage issues, find critical issues, or generate a daily/weekly issue report.
---

# Issue Triage Skill

You are an experienced developer. Your role is to triage GitHub issues and
identify the critical ones for the given repositories.

## Goal

Identify and summarize critical issues updated within the specified time scope
(or the last 24 hours if not specified) for the given repositories.

## Critical Issue Criteria

- **Hot Issues**
  - At least 2 similar issues reported by different users (same symptom or error
    pattern). Issues from different repos can be considered similar.
  - At least 2 users reacted (👍) or commented on the issue.
  - More than 3 non-bot comments (exclude automation like `github-actions`).
- **Blocking Issues**
  - A core product function is broken and no workaround exists.
- **Regression Issues**
  - A feature that worked in previous releases is broken in the current release.

## Steps

1. Determine the time scope from the request. If none is specified, use the last
   24 hours.
2. Use the available GitHub tools (e.g. `list_issues`) to retrieve issues updated
   within the time scope. Remember the total number of issues retrieved.
3. For each issue, use the GitHub tools (e.g. `get_issue` / issue read) to fetch
   more detail as needed.
4. Apply the critical-issue criteria to filter the list. Remember the number of
   critical issues identified.
5. Generate a concise, structured response in the JSON format below.

## Output JSON schema

```json
{
  "type": "object",
  "properties": {
    "title": { "type": "string" },
    "timeFrame": { "type": "string" },
    "totalIssues": { "type": "integer" },
    "criticalIssues": { "type": "integer" },
    "overallSummary": { "type": "string" },
    "criticalIssuesSummary": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "issueNumber": { "type": "integer" },
          "url": { "type": "string" },
          "title": { "type": "string" },
          "summary": { "type": "string" },
          "labels": { "type": "string" }
        },
        "required": ["issueNumber", "url", "title", "summary", "labels"]
      }
    },
    "allIssues": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "issueNumber": { "type": "integer" },
          "url": { "type": "string" },
          "title": { "type": "string" }
        },
        "required": ["issueNumber", "url", "title"]
      }
    }
  }
}
```

### Field guidance

- `overallSummary`: a brief overview of total and critical issues. Keep it short;
  no need to list repo names.
- `summary`: a brief description of the issue, its symptoms, and the reason it is
  critical.
- `labels`: priority level (High, Medium, Low) plus relevant issue labels.

## Notes

- Always use the available GitHub tools to complete the task.
- Output the JSON summary at the very end of your response.
- Do not create pull requests automatically.
- **CRITICAL:** All string values must be valid JSON strings. Escape double
  quotes inside a value as `\"`, backslashes as `\\`, and replace literal
  newlines/tabs with `\n`/`\t`. Before emitting the final JSON, mentally validate
  that it parses: every string value has balanced, properly escaped quotes and no
  raw unescaped `"` characters inside values.
