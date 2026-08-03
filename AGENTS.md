# IssueLens agent instructions

## Foundry development

This project was built with the microsoft-foundry skill. Before working on or
answering questions about Foundry agents, read the microsoft-foundry skill
first.

## GitHub access — use MCP servers only

- For **all** GitHub access — reading issues, comments, and labels, and applying
  labels — use **only** MCP server tools: the `github` MCP server (invocations)
  or the Foundry `toolbox` MCP server (chat).
- The toolbox has Tool Search enabled, so it exposes only `tool_search` and
  `call_tool`. Find a GitHub tool with `tool_search` (e.g. "list issues in a
  repository"), then invoke the exact name it returns via `call_tool`. Those
  names are `<server_label>___<tool>` with three underscores — currently
  `GitHub2___list_issues`, `GitHub2___issue_read`, `GitHub2___issue_write`,
  `GitHub2___search_issues`, `GitHub2___get_label`, and so on.
- If a toolbox call fails with `CONSENT_REQUIRED`, stop and reply with the
  consent URL from the error so the user can authorize access, then retry.
- **Never** shell out to the `gh` CLI, and never run `bash` / `powershell` /
  `shell` commands to perform GitHub operations. Routing every GitHub action
  through an MCP server keeps it attributed to the App bot using its scoped
  installation token (with the App's permissions), not any ambient identity.
- Shell tools may be used only for non-GitHub tasks.
