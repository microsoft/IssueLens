# IssueLens

**Turn GitHub issues into clear priorities and actionable plans.**

IssueLens is an AI agent that helps maintainers spend less time sorting issues
and more time deciding what to do next. It brings together issue history,
repository code, and team knowledge to triage reports and prepare technical
plans for human review.

Built with the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) and
deployable as a [Microsoft Foundry hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents),
IssueLens works through chat or GitHub Actions.

## What it can do

- **Surface critical issues** - identify hot, blocking, and regression reports.
- **Find duplicates** - compare reports using technical evidence, not just similar titles.
- **Organize the backlog** - apply existing labels and route issues to owners.
- **Plan the next step** - produce action plans and design specifications grounded in source and tests.
- **Keep people informed** - send triage reports by email or Teams through configured integrations.
- **Build team memory** - retrieve project knowledge and maintain an evidence-backed GitHub wiki when authorized.

## See it in action

Once connected to a running agent, try requests like these, replacing
`owner/repo` with your repository:

```text
Find critical issues updated in the last 24 hours in owner/repo.

Find duplicates for owner/repo#123.

Create an action plan and design specification for owner/repo#123.
```

You can also use `@issuelens triage` or `@issuelens plan` in a supported
maintainer issue comment. See the [command guide](docs/guide.md#built-in-commands)
for targeting and authorization rules. Planning requests publish artifacts to
the target issue by default.

## Get started

1. **Run the agent** - follow the [local setup](docs/guide.md#running-locally) or
   [Foundry deployment guide](docs/guide.md#deploying-the-agent-to-microsoft-foundry).
   You'll need a model backend and a GitHub App backed by Azure Key Vault.
2. **Connect your repository** - use the [GitHub Actions integration](.github/actions/issuelens/README.md)
   for issue-driven triage, direct tasks, or opt-in post-merge wiki updates.
3. **Make it yours** - optionally [configure repository policies](docs/guide.md#target-repository-configuration)
   for priorities, labels, ownership, planning, and team memory. Built-in defaults
   work without customization files.

GitHub writes use scoped App permissions and are attributed to the App bot.
IssueLens is a **triage and planning agent**, not a coding agent: planning
approval does not implement changes, open pull requests, merge code, or deploy.

## Learn more and contribute

[Setup and usage](docs/guide.md) |
[Contributing](CONTRIBUTING.md) |
[GitHub MCP reference](github_app_mcp/README.md) |
[Observability](docs/observability.md) |
[Security](SECURITY.md) |
[MIT license](LICENSE)

Bug reports, feature ideas, documentation improvements, and pull requests are
welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md); local tests need no Azure
or GitHub credentials.

> **IMPORTANT!** All samples and other resources made available in this GitHub repository ("samples") are designed to assist in accelerating development of agents, solutions, and agent workflows for various scenarios. Review all provided resources and carefully test output behavior in the context of your use case. AI responses may be inaccurate and AI actions should be monitored with human oversight.
