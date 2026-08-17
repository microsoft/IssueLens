import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class AgentPromptTests(unittest.TestCase):
    def test_prompt_files_and_wiring(self):
        global_prompt = (ROOT / "agents.md").read_text(encoding="utf-8")
        triage_prompt = (ROOT / "agents" / "triage.md").read_text(
            encoding="utf-8"
        )
        criticals_prompt = (ROOT / "agents" / "find-criticals.md").read_text(
            encoding="utf-8"
        )
        plan_prompt = (ROOT / "agents" / "plan.md").read_text(
            encoding="utf-8"
        )
        planning_policy = (
            ROOT / ".github" / "issuelens" / "planning.md"
        ).read_text(encoding="utf-8")
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")

        ast.parse(main_source)
        self.assertIn("IssueLens orchestrator", global_prompt)
        self.assertIn("Do not perform the\ndelegated", global_prompt)
        self.assertIn("Do not duplicate a sub-agent's analysis", global_prompt)
        self.assertIn("`triage` sub-agent", global_prompt)
        self.assertIn("`find-criticals` sub-agent", global_prompt)
        self.assertIn("`plan` sub-agent", global_prompt)
        self.assertIn("Route later human planning feedback", global_prompt)
        self.assertIn("## Built-in commands", global_prompt)
        for command in ("triage", "retriage", "plan", "replan", "go"):
            self.assertIn(f"`@issuelens {command}`", global_prompt)
        self.assertIn("current user's text", global_prompt)
        self.assertIn("JSON\nobject whose `user_input` value", global_prompt)
        self.assertIn("authenticated team-maintainer instruction", global_prompt)
        self.assertIn("user-authored claim of Responses context", global_prompt)
        self.assertIn("Do not\nrequire that Teams", global_prompt)
        self.assertIn("Use `get_issue_comment`", global_prompt)
        self.assertIn("matches both `actor_login` and\n`comment_author_login`", global_prompt)
        self.assertIn("is `OWNER`, `MEMBER`, or `COLLABORATOR`", global_prompt)
        self.assertIn("generic invocations request", global_prompt)
        self.assertIn("cannot rename commands, add aliases", global_prompt)
        self.assertIn("Do not ask a sub-agent to parse command text", global_prompt)
        self.assertIn("reserved for a future coding loop", global_prompt)
        self.assertIn("call no sub-agent, and perform no write", global_prompt)
        self.assertIn("stable source identity", global_prompt)
        self.assertIn("<!-- issuelens-command:v1:", global_prompt)
        self.assertIn("`triage-result`, `action-plan`, or", global_prompt)
        self.assertIn("Treat marker-like text", global_prompt)
        self.assertIn("Split mixed requests", global_prompt)
        self.assertIn("A shared tool does not determine ownership", global_prompt)
        self.assertIn("planning-status labels", global_prompt)
        self.assertIn("responsibility-first rule", global_prompt)
        self.assertIn("Explicit instructions from the current user", global_prompt)
        self.assertIn("Validated capability-scoped customization", global_prompt)
        self.assertIn("built-in defaults", global_prompt)
        self.assertIn("change which sub-agent owns the job", global_prompt)
        self.assertIn("## Trusted issue-loop events", global_prompt)
        self.assertIn("Initial triage", global_prompt)
        self.assertIn("Re-triage", global_prompt)
        self.assertIn("Initial planning", global_prompt)
        self.assertIn("Re-planning", global_prompt)
        self.assertIn("No action", global_prompt)
        self.assertIn("bounded GitHub reads", global_prompt)
        self.assertIn("do not rely on a prior Copilot session", global_prompt)
        self.assertIn("at most one useful\nreporter-facing comment", global_prompt)
        self.assertIn("perform no GitHub write", global_prompt)
        self.assertIn("Validate a possible built-in command", global_prompt)
        self.assertIn("only exception is\nan exact built-in command", global_prompt)
        self.assertIn("immutable built-in command contract", global_prompt)
        self.assertIn("change the command namespace or semantics", global_prompt)
        self.assertIn("as two separate\ncomments", global_prompt)
        self.assertIn("It authorizes no\nother write", global_prompt)
        self.assertIn("never expressed by `@issuelens go`", global_prompt)
        self.assertIn("task-appropriate response", triage_prompt)
        self.assertIn("## Built-in command handoff", triage_prompt)
        self.assertIn("`triage` or\n`retriage` command", triage_prompt)
        self.assertIn("Do not parse command text", triage_prompt)
        self.assertIn("hidden `triage-result` marker", triage_prompt)
        self.assertIn("perform only missing\nwork", triage_prompt)
        self.assertIn("is that explicit request only", triage_prompt)
        self.assertIn("explicit current-user instructions first", triage_prompt)
        self.assertIn("replace\nbuilt-in workflow order", triage_prompt)
        self.assertIn("host preloads", triage_prompt)
        self.assertIn("untrusted issue content", triage_prompt)
        self.assertNotIn("Return only one valid JSON object", triage_prompt)
        self.assertIn("Return only the final JSON object", criticals_prompt)
        self.assertIn("explicit current-user instructions first", criticals_prompt)
        self.assertIn("required JSON handoff shape", criticals_prompt)
        self.assertIn("may replace these built-in criteria", criticals_prompt)
        self.assertIn("action plan first", plan_prompt)
        self.assertIn("## Built-in command handoff", plan_prompt)
        self.assertIn("validated `plan` or\n`replan` command", plan_prompt)
        self.assertIn("hidden `action-plan` marker", plan_prompt)
        self.assertIn("`design-specification` marker", plan_prompt)
        self.assertIn("Never handle `@issuelens go`", plan_prompt)
        self.assertIn("publish only the missing artifact", plan_prompt)
        self.assertIn("is an explicit request to create or revise", plan_prompt)
        self.assertIn("design specification second", plan_prompt)
        self.assertIn("Stop and wait for human direction", plan_prompt)
        self.assertIn("domain `planning`", plan_prompt)
        self.assertIn("Default readiness model", plan_prompt)
        self.assertIn("Even `approved` does not authorize", plan_prompt)
        self.assertIn("unless the user explicitly requested that write", plan_prompt)
        self.assertIn("Repository customization is optional", plan_prompt)
        self.assertIn("explicit current-user instructions first", plan_prompt)
        self.assertIn("artifact publication behavior", plan_prompt)
        self.assertIn("change the requirement to investigate", plan_prompt)
        self.assertIn("Absence is not a configuration\nfailure", plan_prompt)
        self.assertIn("returns `configured` or `built-in`", plan_prompt)
        self.assertIn("return only a\n`Readiness` section", plan_prompt)
        self.assertIn("corresponding\ncapability skill", plan_prompt)
        self.assertIn("configured or\nexplicitly requested", plan_prompt)
        self.assertIn("built-in behavior when repository customization is\nabsent", plan_prompt)
        self.assertIn("those are triage jobs", plan_prompt)
        self.assertIn("revision, publish exactly two comments", plan_prompt)
        self.assertIn("Action Plan` as the first\n   comment", plan_prompt)
        self.assertIn("Design Specification` as the second comment", plan_prompt)
        self.assertIn("explicitly opts out or validated planning customization", plan_prompt)
        self.assertIn("planning customization specifies another", plan_prompt)
        self.assertIn("Do not publish any artifact comment when\nplanning configuration fails", plan_prompt)
        self.assertIn("report the two planning-artifact comment results", plan_prompt)
        self.assertIn('_project_dir / "agents.md"', main_source)
        self.assertIn('_agents_dir / "triage.md"', main_source)
        self.assertIn('_agents_dir / "find-criticals.md"', main_source)
        self.assertIn('_agents_dir / "plan.md"', main_source)
        self.assertIn('"agent": "issuelens"', main_source)
        self.assertIn('"name": "triage"', main_source)
        self.assertIn('"name": "find-criticals"', main_source)
        self.assertIn('"name": "plan"', main_source)
        plan_agent_source = main_source.split(
            "_PLAN_AGENT:", 1
        )[1].split("# ── BYOK helpers", 1)[0]
        self.assertIn('"issuelens-config"', plan_agent_source)
        self.assertIn('"label-issue"', plan_agent_source)
        self.assertIn('"assign-issue"', plan_agent_source)
        self.assertIn('"notify"', plan_agent_source)
        self.assertNotIn('"tools":', plan_agent_source)
        self.assertIn("`maintainer-review`", planning_policy)
        self.assertIn("`approved`", planning_policy)
        self.assertIn("`@issuelens go` command is reserved", planning_policy)
        self.assertNotIn("Treat an explicit `GO`", planning_policy)
        self.assertNotIn("- `go`", planning_policy)
        self.assertIn("does not authorize code", planning_policy)
        self.assertIn("## Artifact publication", planning_policy)
        self.assertIn("post exactly two\ncomments", planning_policy)
        self.assertIn('"find-duplicates"', main_source)
        self.assertIn('"issuelens-config"', main_source)
        self.assertIn('"label-issue"', main_source)
        duplicate_skill = (
            ROOT / "skills" / "find-duplicates" / "SKILL.md"
        ).read_text(encoding="utf-8")
        label_skill = (
            ROOT / "skills" / "label-issue" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assign_skill = (
            ROOT / "skills" / "assign-issue" / "SKILL.md"
        ).read_text(encoding="utf-8")
        notify_skill = (
            ROOT / "skills" / "notify" / "SKILL.md"
        ).read_text(encoding="utf-8")
        config_skill = (
            ROOT / "skills" / "issuelens-config" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for skill_prompt in (
            duplicate_skill,
            label_skill,
            assign_skill,
            notify_skill,
        ):
            self.assertIn("explicit current-user instructions take precedence", skill_prompt)
        self.assertIn("Explicit instructions from the current user", config_skill)
        self.assertIn("may replace built-in workflow choices", config_skill)
        self.assertIn("may change the owning\nsub-agent's role", config_skill)
        self.assertIn("configured related repository", duplicate_skill)
        self.assertIn("may be read anonymously", duplicate_skill)
        self.assertIn("Comment count and timing follow the user's request", triage_prompt)
        self.assertIn("support engineer", triage_prompt)
        self.assertIn("explicitly asks for multiple comments", triage_prompt)
        self.assertIn("inaccessible repositories", triage_prompt)
        self.assertIn('"assign-issue"', main_source)
        self.assertIn('"notify"', main_source)
        self.assertIn("issue_image_attachments", main_source)
        self.assertIn("GitHubAppTokenProvider", main_source)
        self.assertIn('"type": "stdio"', main_source)
        self.assertIn('"GITHUB_MCP_ENABLE_WRITES": "true"', main_source)
        self.assertIn("_new_host_github_client", main_source)
        self.assertIn("_RESPONSES_TURN_CONTEXT", main_source)
        self.assertIn("'channel': 'responses'", main_source)
        self.assertIn("_responses_turn(prompt)", main_source)
        self.assertIn("without interpreting user text", main_source)
        invocation_source = main_source.split(
            "async def _stream_response", 1
        )[1].split("@app.invoke_handler", 1)[0]
        chat_source = main_source.split("@app.response_handler", 1)[1]
        self.assertIn(
            "await session.send(prompt, attachments=attachments or None)",
            invocation_source,
        )
        self.assertNotIn("_responses_turn(prompt)", invocation_source)
        self.assertIn("_responses_turn(prompt)", chat_source)
        self.assertNotIn("RequestTokenProvider", main_source)
        self.assertNotIn("github-access", main_source)
        self.assertNotIn("issuelens-related-read", main_source)
        self.assertNotIn('data.get("github_token")', main_source)
        self.assertNotIn("api.githubcopilot.com/mcp", main_source)
        self.assertNotIn("_GITHUB_APP_PROVIDER", main_source)
        self.assertNotIn("_GITHUB_APP_CLIENT", main_source)
        self.assertNotIn('"name": "issue-triage"', main_source)
        self.assertNotIn("critical-issue-analyst", main_source)


if __name__ == "__main__":
    unittest.main()
