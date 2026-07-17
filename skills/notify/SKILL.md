---
name: notify
description: Send an issue triage report or notification to people via WorkIQ (Microsoft 365) — as an email or a Teams message. Use after triaging issues when asked to notify, send a report, email the summary, or message the team.
---

# Notify Skill

Deliver a triage report or notification through **WorkIQ**, the Microsoft 365
tool surface. WorkIQ tools are exposed by the `workiq` MCP server, so their names
are **prefixed with `workiq-`** (e.g. `workiq-do_action`, `workiq-fetch`,
`workiq-search_paths`). Scan the available tools list for entries ending in
`do_action` / `fetch` and call those exact names.

## Inputs

- The report content (typically the JSON summary produced by the triage skill,
  and/or a human-readable summary).
- A target: an email address, a person's name, or a Teams chat/channel. If no
  target is provided, ask for one or use the configured default recipient.

## Sending an email (default)

1. Compose a clear subject (e.g. `Daily Issue Triage Report — <timeFrame>`) and an
   HTML or plain-text body summarizing the critical issues with links.
2. Call the WorkIQ action to send mail: `do_action` on `/me/sendMail` with the
   recipient(s), subject, and body.
3. Confirm the send succeeded from the tool response (a 2xx / accepted result).

## Sending a Teams message

1. Resolve the target chat or channel first with `fetch` (e.g. list `/chats` or
   the team's channels) to get its id. Do not guess ids.
2. Call `do_action` to post the message to the resolved chat/channel.
3. Confirm success from the tool response.

## Rules

- **Report honestly.** Only claim the notification was sent if the tool response
  confirms it. If the WorkIQ tools are unavailable (no `workiq-*` tools present)
  or a call fails, say so explicitly — do not fabricate success.
- Resolve-then-act: when targeting a named person, chat, or channel, resolve the
  id with `fetch` before sending.
- Keep the message concise: an overall summary line plus the list of critical
  issues with their URLs.
