---
name: assign-issue
description: Assign GitHub issues to the right person based on technical area ownership and historical patterns. Use when auto-assigning new issues, finding the right owner, or routing issues to area experts. Triggers on "assign issue", "find owner for issue", "who should handle this issue", and "route this issue".
---

# Assign Issue Skill

Assign a GitHub issue to an appropriate owner using repository-defined area
ownership and historical assignment patterns.

## Inputs

- An issue reference (`owner/repo` and issue number, or an issue URL).
- Optional assignment constraints supplied by the user.

## Assignment Strategies

### Static area mapping

Look for an area-owner file in this order:

1. `.github/area_owners.md`
2. `docs/area_owners.md`
3. `area_owners.md`

Match the issue title, body, labels, and mentioned file paths against the
areas' keywords and path patterns. The format is described in
[references/area_owners_template.md](references/area_owners_template.md).

### Historical patterns

Use the GitHub MCP issue-search tools to find similar closed issues in the same
repository:

1. Search using the most specific terms from the issue title and labels.
2. Prefer issues updated in the last 6 to 12 months.
3. Inspect up to 20 relevant results and count their assignees.
4. Rank candidates by assignment frequency and similarity to the target issue.

Do not infer ownership from issue authorship alone.

## Workflow

1. Read the target issue and preserve its existing assignees.
2. Apply the static area mapping when an area-owner file exists.
3. Analyze similar closed issues for historical assignee patterns.
4. Combine the evidence:
   - Both strategies agree: high confidence.
   - One strategy produces a clear match: medium confidence.
   - Strategies conflict or evidence is weak: low confidence; report candidates
     and do not assign automatically.
5. For a medium- or high-confidence individual user, update the issue with the
   GitHub MCP issue-write tool using `method: update`. Pass the union of existing
   assignees and the selected username so no current assignment is removed.
6. Read the issue again and confirm that the selected assignee is present.
7. Report the decision, evidence, and whether the write was confirmed.

## Rules

- Use only the GitHub MCP server for every read, search, and write. Never use a
  shell command, bundled script, direct HTTP request, or GitHub CLI command.
- Never remove or replace existing assignees.
- Do not assign a team handle directly; GitHub issue assignees must be
  individual users. Resolve a team to an eligible individual only when the
  available GitHub tools provide enough evidence; otherwise report the team as
  a routing suggestion.
- Treat a failed or unconfirmed write as a suggestion, not a completed action.
- If no area-owner file exists, use historical patterns only.
- If no confident match exists, do not guess and do not modify the issue.

## Output

- **Assigned to:** the selected username, or `None`.
- **Confidence:** High, Medium, or Low.
- **Reasoning:** the area match and historical evidence.
- **Existing assignees:** assignees preserved by the update.
- **Action taken:** assignment confirmed, assignment failed, or manual review
  suggested.