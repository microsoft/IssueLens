# IssueLens Notification Content Policy

Use this policy only to compose notification content. Recipients, channels, and
authorization to send must come from the user's explicit request.

## Title

Use `IssueLens Issue Triage Report` for a general report. For a repository- or
time-specific report, append concise context without including credentials or
internal endpoint information.

## Content Order

1. State the time frame and total number of issues analyzed.
2. State the number of critical issues and summarize the overall result in one
   sentence.
3. List critical issues first, ordered by impact and then by issue number.
4. For each critical issue, include its number, linked title, classification
   (`hot`, `blocking`, or `regression`), concrete evidence, and existing labels.
5. Include non-critical issues only as a compact count or linked list when the
   user requested a complete report.
6. End with confirmed label or assignment actions, clearly separated from
   recommendations and failures.

## Presentation

- Keep Teams messages concise and use Markdown links and short bullets or a
  compact table.
- Use accessible inline-styled HTML for email with text links and simple tables.
- Do not use color alone to communicate severity.
- Distinguish `No critical issues found` from `Triage could not complete`.
- State tool or configuration failures plainly and never imply an action
  succeeded without confirmation.

Do not include GitHub tokens, App installation details, private-key locations,
Logic App endpoints, raw exception secrets, or other credentials in a
notification.