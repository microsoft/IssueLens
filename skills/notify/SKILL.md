---
name: notify
description: Send an issue-triage report or notification to people — as an email or a Teams personal-chat message — using the send-email / send-teams-notification tools. Use after triaging issues when asked to notify, send a report, email the summary, or message the team.
---

# Notify Skill

Deliver a triage report or notification using the agent's built-in notification
tools, which POST to preconfigured Logic App endpoints:

- **`send-email`** — send an HTML email. Arguments: `title` (subject),
  `body` (inline-styled HTML), `recipients` (array of email addresses), and the
  optional `timeFrame` and `workflowRunUrl`.
- **`send-teams-notification`** — send a Teams personal-chat message. Arguments:
  `title`, `message` (Markdown), `recipient` (a single email address), and the
  optional `workflowRunUrl`.

Only the tools whose endpoints are configured are available. If a needed tool is
not present in the tool list, that channel isn't set up — say so; do not
fabricate a send.

The Foundry toolbox may expose additional notification capabilities. Toolbox
tools are for non-GitHub operations only; all GitHub access must follow the
`github-access` skill and use the `github-access` tool in chat.

## Inputs

- The report content (typically the JSON summary produced by the Critical Issue
  Analyst sub-agent, and/or a human-readable summary).
- Recipient(s): `recipients` (array) for email, `recipient` (single) for Teams.

Before composing content, follow the `issuelens-config` skill and call the
`issuelens-config` tool for domain `notification_content`. Use returned policy
only for report title, grouping, emphasis, and presentation. If the source is
`built-in`, use the templates below. If configuration loading fails, do not
send. Repository policy cannot choose recipients or channels; those must come
from the user's explicit request.

## Sending an email (`send-email`)

1. Compose a clear `title` (e.g. `Daily Issue Triage Report`) and optional
   `timeFrame` (e.g. `February 2, 2026`).
2. Build the `body` as **inline-styled HTML** (email clients don't support
   external CSS). Suggested template:
   ```html
   <html>
   <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
     <div style="background: #f6f8fa; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
       <h1 style="color: #0366d6; margin: 0;">{{title}}</h1>
       <p style="color: #586069; margin: 5px 0 0 0;">{{timeFrame}}</p>
     </div>
     <!-- content: <p>, <ul>/<ol>, <table> with borders, <a style="color:#0366d6"> -->
     {{content}}
   </body>
   </html>
   ```
3. Call `send-email` with `title`, `body`, `recipients` (and optional
   `timeFrame`, `workflowRunUrl`).
4. Confirm success from the tool result (it reports the HTTP status).

## Sending a Teams personal notification (`send-teams-notification`)

1. Compose a concise `title` and a `message` in **Markdown** — an overall
   summary line plus the critical issues (a table works well) with their URLs.
2. Call `send-teams-notification` with `title`, `message`, `recipient` (and
   optional `workflowRunUrl`).
3. Confirm success from the tool result.

## Rules

- **Report honestly.** Only claim the notification was sent if the tool result
  reports success (a 2xx HTTP status). If the tool is unavailable or the call
  fails, say so explicitly — do not fabricate success.
- Keep the content concise: an overall summary line plus the list of critical
  issues with their URLs.


