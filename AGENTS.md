# IssueLens agent instructions

## Foundry development

This project was built with the microsoft-foundry skill. Before working on or
answering questions about Foundry agents, read the microsoft-foundry skill
first.

## GitHub access — use the GitHub MCP server only

- For **all** GitHub access — reading issues, comments, and labels, and applying
  labels — use **only** the GitHub MCP server tools (the `github` MCP server).
- **Never** shell out to the `gh` CLI, and never run `bash` / `powershell` /
  `shell` commands to perform GitHub operations. Routing every GitHub action
  through the MCP server keeps it attributed to the App bot using its scoped
  installation token (with the App's permissions), not any ambient identity.
- Shell tools may be used only for non-GitHub tasks.
