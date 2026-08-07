---
name: label-issue
description: Classify and label a GitHub issue based on repository-specific labeling instructions. Use when auto-labeling issues, classifying issue types (bug, feature, question), or adding priority/area labels. Triggers on "label issue", "classify issue", "what labels should this issue have".
---

# Label Issue Skill

Automatically classify and apply labels to a GitHub issue using the available
GitHub tools and repository-specific labeling rules.

## Workflow

1. **Input:** an issue reference (`owner/repo` + issue number, or an issue URL).
2. **Fetch labeling instructions:** read `.github/label-instructions.md` from the
   target repository using the available GitHub tools. If it is not present, fall
   back to the repository's existing label set (list the repo labels) and use
   their names/descriptions to guide classification.
3. **Fetch the issue:** read the issue title, body, existing labels, and comments
   with the GitHub tools.
4. **Analyze the issue:**
   - **Type detection** — bug, feature, question, etc. (match keywords against
     label names/descriptions).
   - **Priority** — identify severity indicators (core function broken, no
     workaround → high).
   - **Area/component** — match content against area-specific labels.
5. **Apply labels:** use the available add-labels operation to add the selected
   labels. Do not remove existing labels.
6. **Report:** list the labels applied and the reasoning for each.

## Rules

- Only add labels that exist in the repository. Never invent labels.
- Preserve existing labels; only add.
- Prefer the repository's `.github/label-instructions.md` rules when present.
- Follow the `github-access` skill for every GitHub read or write. Use
   request-scoped GitHub MCP tools for invocations or the `github-access` tool
   for chat. Never use shell commands, direct HTTP, or the GitHub CLI.

## Output

Report the labeling decision:

- **Labels applied:** the labels added.
- **Reasoning:** why each label was chosen (type / priority / area).
- **Existing labels:** labels already present (left unchanged).
