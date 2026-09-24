"""Approval/CI gates and bounded verification around the official azd deploy."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "microsoft/IssueLens"
ENVIRONMENT = "foundry-production"
SERVICE = "IssueLens"
ACKNOWLEDGEMENT = "ISSUELENS_DEPLOYMENT_OK"
SMOKE_INPUT = (
    f"Reply with exactly {ACKNOWLEDGEMENT}, with no other text. "
    "This is a deployment smoke check, not a triage or planning task. "
    "Do not call any tools or sub-agents, read repository content, modify GitHub "
    "or wiki data, or send notifications. No repository or write is authorized."
)
MAX_OUTPUT = 8 * 1024 * 1024
MAX_PACKAGE = 250 * 1024 * 1024
CI_JOBS = {
    "Application tests (Python 3.13)",
    "MCP tests (Python 3.12)",
    "MCP tests (Python 3.13)",
    "MCP package build/install (Python 3.12)",
    "MCP package build/install (Python 3.13)",
    "Workflow validation",
}
CONFIG_FIELDS = (
    "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID",
    "AZURE_LOCATION", "AZURE_AI_PROJECT_ID", "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME", "AZURE_AI_MODEL_API_KEY",
    "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_SECRET_URI",
    "TOOLBOX_ENDPOINT", "MAILING_URL", "PERSONAL_NOTIFICATION_URL",
)
OPTIONAL_FIELDS = {
    "AZURE_AI_MODEL_API_KEY", "TOOLBOX_ENDPOINT",
    "MAILING_URL", "PERSONAL_NOTIFICATION_URL",
}
SECRET_FIELDS = {"AZURE_AI_MODEL_API_KEY", "MAILING_URL", "PERSONAL_NOTIFICATION_URL"}

_spec = importlib.util.spec_from_file_location(
    "issuelens_action", ROOT / ".github" / "actions" / "issuelens" / "issuelens_action.py",
)
assert _spec is not None and _spec.loader is not None
_action = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_action)
require = _action.require


def scratch(name):
    return Path(os.environ["RUNNER_TEMP"]).resolve() / name


def read_state():
    path = scratch("issuelens-deployment.json")
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(state, dict), "Invalid deployment receipt")
    return state


def record(**values):
    state = read_state()
    state.update(values)
    scratch("issuelens-deployment.json").write_text(json.dumps(state), encoding="utf-8")


def cli(arguments, label, timeout=60, env=None):
    # CLI definitions and raw protocol output can contain secrets. Never echo them,
    # including subprocess exceptions; temporary streams are not uploaded.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(
                arguments, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(f"{label} timed out; remote operations may still be running") from None
        except OSError:
            raise ValueError(f"{label} could not start; check the installed tooling") from None
        require(result.returncode == 0, f"{label} failed (exit code {result.returncode}); raw output withheld")
        require(stdout.seek(0, os.SEEK_END) + stderr.seek(0, os.SEEK_END) <= MAX_OUTPUT,
                f"{label} exceeded the output limit")
        stdout.seek(0)
        output = stdout.read(MAX_OUTPUT + 1)
        require(len(output) <= MAX_OUTPUT, f"{label} exceeded the output limit")
        return output


def cli_json(arguments, label):
    try:
        value = json.loads(cli(arguments, label), object_pairs_hook=_action.unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"{label} returned invalid JSON") from None
    require(isinstance(value, dict), f"{label} returned an invalid object")
    return value


def github(path, label):
    try:
        result = _action.github_read(f"/repos/{REPOSITORY}{path}")
    except urllib.error.HTTPError as error:
        raise ValueError(f"{label}: GitHub HTTP {error.code}; verify read access and configuration") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError(f"{label}: GitHub could not be reached") from None
    require(isinstance(result, dict), f"{label}: GitHub returned an invalid object")
    return result


def checked_sha():
    sha = _action.full_sha(os.environ["GITHUB_SHA"])
    require(cli(["git", "rev-parse", "HEAD"], "Read checkout SHA").decode().strip() == sha,
            "Checkout does not match the dispatched commit")
    return sha


def preflight():
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "Unexpected deployment repository")
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Deployment requires manual dispatch")
    sha = checked_sha()
    repository = github("", "Read default branch")
    branch = repository.get("default_branch")
    require(isinstance(branch, str) and branch, "Repository default branch is unavailable")
    require(os.environ.get("GITHUB_REF") == f"refs/heads/{branch}", "Deployment requires the default branch")
    comparison = github(f"/compare/{sha}...{urllib.parse.quote(branch, safe='')}?per_page=1", "Verify commit ancestry")
    require(comparison.get("status") in {"ahead", "identical"}
            and isinstance(comparison.get("merge_base_commit"), dict)
            and comparison.get("merge_base_commit", {}).get("sha") == sha,
            "The dispatched commit is no longer on the default branch")

    environment = github(f"/environments/{ENVIRONMENT}", "Read deployment environment")
    require(environment.get("name") == ENVIRONMENT, "Unexpected deployment environment")
    protections = environment.get("protection_rules")
    require(isinstance(protections, list) and all(isinstance(rule, dict) for rule in protections),
            "Deployment environment returned invalid protection rules")
    reviews = [rule for rule in protections if rule.get("type") == "required_reviewers"]
    require(len(reviews) == 1 and reviews[0].get("reviewers")
            and reviews[0].get("prevent_self_review") is True,
            "Configure required environment reviewers with self-review disabled")
    require(environment.get("can_admins_bypass") is False, "Disable environment protection bypass")
    require(environment.get("deployment_branch_policy") == {
        "protected_branches": False, "custom_branch_policies": True,
    }, "Configure a selected-branch deployment policy")
    policies = github(f"/environments/{ENVIRONMENT}/deployment-branch-policies?per_page=100",
                      "Read deployment branch policy")
    rules = policies.get("branch_policies", [])
    require(isinstance(rules, list) and policies.get("total_count") == len(rules) == 1
            and isinstance(rules[0], dict)
            and rules[0].get("name") == branch and rules[0].get("type") == "branch",
            "The environment must allow only the exact default branch, with no tags")

    query = urllib.parse.urlencode({"event": "push", "branch": branch, "head_sha": sha, "per_page": 1})
    runs = github(f"/actions/workflows/ci.yml/runs?{query}", "Read exact-commit CI").get("workflow_runs", [])
    require(isinstance(runs, list) and len(runs) == 1 and isinstance(runs[0], dict),
            "No valid CI run found for this default-branch commit; wait for CI before dispatching")
    run = runs[0]
    require(run.get("path") == ".github/workflows/ci.yml"
            and run.get("head_sha") == sha and run.get("head_branch") == branch
            and run.get("event") == "push"
            and isinstance(run.get("head_repository"), dict)
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "CI run provenance does not match this deployment")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "The latest CI run/attempt for this commit has not succeeded; finish CI before deployment")
    run_id, attempt = _action.positive(run["id"]), _action.positive(run["run_attempt"])
    result = github(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "Read required CI jobs")
    jobs = result.get("jobs", [])
    require(isinstance(jobs, list) and result.get("total_count") == len(jobs) == len(CI_JOBS)
            and all(isinstance(job, dict) and isinstance(job.get("name"), str) for job in jobs)
            and {job.get("name") for job in jobs} == CI_JOBS
            and all(job.get("status") == "completed" and job.get("conclusion") == "success"
                    and job.get("head_sha") == sha for job in jobs),
            "Required CI jobs are missing, incomplete, skipped, or unsuccessful")
    print(f"Protected environment and CI run {run_id}, attempt {attempt}, verified for {sha}.")


def https_url(value, label):
    require(isinstance(value, str), f"Invalid HTTPS URL in {label}")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"Invalid HTTPS URL in {label}") from None
    require(parsed.scheme == "https" and parsed.hostname
            and parsed.username is None and parsed.password is None
            and port in {None, 443} and not parsed.fragment
            and not any(character.isspace() for character in value), f"Invalid HTTPS URL in {label}")
    return parsed


def configuration(environ):
    require(isinstance(environ, Mapping), "Deployment configuration must be an object")
    values = {key: environ.get(key, "") for key in CONFIG_FIELDS}
    for key, value in values.items():
        require(isinstance(value, str) and all(" " <= char <= "~" for char in value),
                f"{key} must contain only printable ASCII without line breaks")
        require(key in OPTIONAL_FIELDS or value.strip(), f"Missing deployment configuration: {key}")
        require(value == value.strip(), f"Remove surrounding whitespace from {key}")
    uuid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    for key in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"):
        require(re.fullmatch(uuid, values[key]), f"{key} must be a UUID")
    project = re.fullmatch(
        rf"/subscriptions/({uuid})/resourceGroups/([A-Za-z0-9_.()-]+)/providers/"
        r"Microsoft\.CognitiveServices/accounts/([A-Za-z0-9_-]+)/projects/([A-Za-z0-9_-]+)",
        values["AZURE_AI_PROJECT_ID"], re.IGNORECASE,
    )
    require(project and project[1].lower() == values["AZURE_SUBSCRIPTION_ID"].lower(),
            "AZURE_AI_PROJECT_ID must identify a Foundry project in the configured subscription")
    endpoint = https_url(values["FOUNDRY_PROJECT_ENDPOINT"], "FOUNDRY_PROJECT_ENDPOINT")
    require(endpoint.hostname.endswith(".services.ai.azure.com")
            and endpoint.path == f"/api/projects/{project[4]}" and not endpoint.query,
            "FOUNDRY_PROJECT_ENDPOINT must identify the configured project on Azure public cloud")
    require(re.fullmatch(r"[a-z0-9]+", values["AZURE_LOCATION"]), "Invalid AZURE_LOCATION")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", values["AZURE_AI_MODEL_DEPLOYMENT_NAME"]),
            "Invalid AZURE_AI_MODEL_DEPLOYMENT_NAME")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", values["GITHUB_APP_ID"]), "Invalid GITHUB_APP_ID")
    vault = https_url(values["GITHUB_APP_PRIVATE_KEY_SECRET_URI"], "GITHUB_APP_PRIVATE_KEY_SECRET_URI")
    require(vault.hostname.endswith(".vault.azure.net") and not vault.query
            and re.fullmatch(r"/secrets/[A-Za-z0-9-]+(?:/[a-fA-F0-9]{32})?", vault.path),
            "Configure a Key Vault secret URI, never the App private key")
    for key in ("TOOLBOX_ENDPOINT", "MAILING_URL", "PERSONAL_NOTIFICATION_URL"):
        if values[key]:
            https_url(values[key], key)
    if values["TOOLBOX_ENDPOINT"]:
        require(values["TOOLBOX_ENDPOINT"].startswith(values["FOUNDRY_PROJECT_ENDPOINT"] + "/toolboxes/"),
                "TOOLBOX_ENDPOINT must belong to the configured Foundry project")
    return values


def prepare():
    values = configuration(os.environ)
    require(not (ROOT / ".azure").exists(), "Expected a fresh runner checkout without .azure state")
    path = scratch("issuelens-deployment-config.json")
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(values, stream)
    record(commit=checked_sha(), environment=ENVIRONMENT, target=values["FOUNDRY_PROJECT_ENDPOINT"],
           publication="not-started", invocations="not-run", responses="not-run")
    print("Deployment configuration validated; credentials remain runner-local.")


def configure():
    values = configuration(json.loads(scratch("issuelens-deployment-config.json").read_text(encoding="utf-8")))
    account = cli_json(["az", "account", "show", "--output", "json"], "Read Azure login")
    require(isinstance(account.get("id"), str) and isinstance(account.get("tenantId"), str)
            and account["id"].lower() == values["AZURE_SUBSCRIPTION_ID"].lower()
            and account.get("tenantId", "").lower() == values["AZURE_TENANT_ID"].lower(),
            "Azure login does not match the configured subscription and tenant")
    project = cli_json(["az", "resource", "show", "--ids", values["AZURE_AI_PROJECT_ID"], "--output", "json"],
                       "Read existing Foundry project")
    require(isinstance(project.get("id"), str) and project["id"].lower() == values["AZURE_AI_PROJECT_ID"].lower()
            and project.get("location") == values["AZURE_LOCATION"],
            "Foundry project identity or location does not match configuration")
    properties = project.get("properties")
    require(isinstance(properties, dict), "Foundry project returned invalid properties")
    endpoints = properties.get("endpoints", {})
    require(isinstance(endpoints, dict)
            and values["FOUNDRY_PROJECT_ENDPOINT"] in
            [value.rstrip("/") for value in endpoints.values() if isinstance(value, str)],
            "The configured endpoint was not returned by the existing Foundry project")
    existing = cli_json([
        "az", "rest", "--method", "get", "--url",
        values["FOUNDRY_PROJECT_ENDPOINT"] + f"/agents/{SERVICE}?api-version=v1",
        "--resource", "https://ai.azure.com", "--output", "json",
    ], "Read existing IssueLens agent")
    require(existing.get("name") == SERVICE, "The target must already contain the IssueLens agent")
    require(not (ROOT / ".azure").exists(), "Refusing to overwrite existing azd state")
    cli(["azd", "config", "set", "auth.useAzCliAuth", "true"], "Configure azd OIDC authentication")
    record(owns_azure_state=True)
    cli(["azd", "env", "new", ENVIRONMENT, "--subscription", values["AZURE_SUBSCRIPTION_ID"],
         "--location", values["AZURE_LOCATION"], "--no-prompt"], "Create runner-local azd environment")
    with tempfile.TemporaryDirectory(prefix="issuelens-env-", dir=scratch(".")) as directory:
        path = Path(directory) / "deployment.env"
        encoded = {key: json.dumps(value).replace("$", r"\$") for key, value in values.items()}
        path.write_text("".join(f"{key}={value}\n" for key, value in encoded.items()), encoding="utf-8")
        os.chmod(path, 0o600)
        cli(["azd", "env", "set", "--file", str(path), "--environment", ENVIRONMENT, "--no-prompt"],
            "Load azd deployment configuration")
    loaded = cli_json(["azd", "env", "get-values", "--environment", ENVIRONMENT, "--output", "json"],
                      "Verify loaded azd configuration")
    require(all(loaded.get(key) == value for key, value in values.items()),
            "azd configuration did not preserve the supplied values")
    scratch("issuelens-deployment-config.json").unlink()
    print("Configured the existing Foundry project and IssueLens agent; no resources were provisioned.")


def required_runtime_files():
    paths = cli(["git", "ls-files", "-z"], "Read tracked runtime files").decode("utf-8").split("\0")
    return {
        name for name in paths if name and (
            "/" not in name and (name.endswith(".py") or name == "requirements.txt")
            or name.startswith(("agents/", "skills/", "schemas/", "github_app_mcp/src/"))
        )
    }


def inspect_package(path, required, secret_values=()):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_PACKAGE,
            "Invalid or oversized code-deployment ZIP")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        require(len(entries) <= 10000 and sum(entry.file_size for entry in entries) <= 2 * MAX_PACKAGE,
                "Code package exceeds inspection limits")
        names = [entry.filename for entry in entries]
        require(len(names) == len(set(names)), "Code package contains duplicate paths")
        require(required <= set(names), "Code package is missing tracked runtime files")
        for entry in entries:
            name = PurePosixPath(entry.filename)
            require(entry.filename == entry.orig_filename and not name.is_absolute()
                    and ".." not in name.parts and "\\" not in entry.filename
                    and ":" not in entry.filename
                    and not stat.S_ISLNK(entry.external_attr >> 16) and not entry.flag_bits & 1,
                    "Code package contains an unsafe path or entry")
            require(not set(name.parts) & {".git", ".github", ".azure", ".foundry", ".venv", ".issuelens-copilot"}
                    and not name.name.lower().endswith((".pem", ".pfx", ".p12"))
                    and (not name.name.startswith(".env") or name.name == ".env.example"),
                    "Code package contains credentials, local state, or excluded development files")
            if entry.is_dir():
                continue
            content = archive.read(entry)
            require(not any(secret and secret.encode("utf-8") in content for secret in secret_values),
                    "Code package contains a configured runtime secret")
            if entry.filename in required:
                require(content == (ROOT / entry.filename).read_bytes(),
                        "Packaged runtime content does not match the approved checkout")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_check():
    directory = scratch("issuelens-code-package")
    require(Path(os.environ["ISSUELENS_PACKAGE_CHECK_DIR"]).resolve() == directory
            and directory.is_dir() and not directory.is_symlink(), "Invalid isolated package directory")
    # The pinned Foundry provider creates exactly one such archive for our one
    # service. Checking in postpackage retains its code-zip metadata for deploy.
    archives = list(directory.glob("azd-code-deploy-*.zip"))
    require(len(archives) == 1, "Expected exactly one Foundry code ZIP before upload")
    checked_sha()
    cli(["git", "diff", "--exit-code", "HEAD", "--"], "Verify clean approved source")
    required = required_runtime_files()
    require({"main.py", "requirements.txt", "agents/issuelens.md",
             "github_app_mcp/src/issuelens_github_mcp/server.py"} <= required,
            "Required IssueLens runtime sources are unavailable")
    digest = inspect_package(archives[0], required, [os.environ.get(key, "") for key in SECRET_FIELDS])
    record(package_sha256=digest)
    print(f"Verified the deployment ZIP against {len(required)} tracked runtime files.")


def publish():
    checked_sha()
    directory = scratch("issuelens-code-package")
    directory.mkdir()
    environment = {**os.environ, "TMPDIR": str(directory), "TEMP": str(directory), "TMP": str(directory),
                   "ISSUELENS_PACKAGE_CHECK_DIR": str(directory)}
    record(publication="attempted")
    cli(["azd", "deploy", SERVICE, "--environment", ENVIRONMENT, "--no-prompt", "--timeout", "1200"],
        "Foundry deployment", timeout=1260, env=environment)
    require(read_state().get("package_sha256"), "Deployment returned without a package-inspection receipt")
    record(publication="completed")
    print("azd deployment completed; protocol verification is still required.")


def http_response(raw):
    header, separator, body = raw.partition(b"\r\n\r\n")
    require(separator and len(header) <= 64 * 1024, "Missing or oversized HTTP response headers")
    status_line, separator, headers = header.partition(b"\r\n")
    require(separator and re.fullmatch(rb"HTTP/\d(?:\.\d)? 200(?: [^\r\n]*)?", status_line),
            "Smoke request did not return HTTP 200")
    message = BytesParser().parsebytes(headers + b"\r\n\r\n")
    require(message.get_content_type() == "text/event-stream", "Smoke request did not return SSE")
    stream = io.BytesIO(body)
    return SimpleNamespace(headers=message, readline=stream.readline, read=stream.read), body


class NoTools:
    def tick(self):
        pass

    def event(self, event):
        data = event.get("data", {})
        require(isinstance(data, dict), "Invalid smoke invocation event")
        require(not str(event.get("type", "")).startswith(("tool.", "subagent."))
                and not data.get("toolRequests"), "Smoke invocation unexpectedly requested tools")


def responses_text(body):
    require(len(body) <= MAX_OUTPUT, "Responses stream exceeds its limit")
    completed = None
    data_lines = []
    event_name = ""
    for line in body.decode("utf-8").splitlines():
        require(len(line) <= 1024 * 1024, "Responses event exceeds its limit")
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif not line and data_lines:
            data = "\n".join(data_lines)
            data_lines = []
            if data == "[DONE]":
                require(completed is not None, "Responses ended before completion")
                continue
            require(completed is None, "Responses returned events after completion")
            event = json.loads(data, object_pairs_hook=_action.unique_object)
            require(isinstance(event, dict), "Invalid Responses event")
            kind = event.get("type")
            require(isinstance(kind, str) and kind, "Responses returned an invalid event type")
            failures = {"error", "response.failed", "response.incomplete", "response.cancelled"}
            require(kind not in failures and event_name not in failures,
                    "Responses reported an execution error")
            if kind == "response.completed":
                completed = event.get("response")
            event_name = ""
    require(not data_lines and isinstance(completed, dict), "Responses stream ended without complete terminal data")
    require(completed.get("status") == "completed" and isinstance(completed.get("id"), str) and completed["id"]
            and not completed.get("error") and not completed.get("incomplete_details"),
            "Responses did not complete successfully")
    output = completed.get("output")
    require(isinstance(output, list) and all(isinstance(item, dict) for item in output),
            "Responses returned an invalid output list")
    text = []
    for item in output:
        require(item.get("type") in {"message", "reasoning"}, "Responses unexpectedly returned a tool call")
        if item.get("type") == "message":
            require(item.get("role") == "assistant" and item.get("status") == "completed",
                    "Responses returned an incomplete assistant message")
            content = item.get("content")
            require(isinstance(content, list), "Responses returned invalid message content")
            for part in content:
                require(isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str),
                        "Responses returned unexpected content")
                text.append(part["text"])
    return "".join(text)


def verify():
    require(read_state().get("publication") == "completed", "Verification requires a completed deployment receipt")
    version = cli(["azd", "env", "get-value", "AGENT_ISSUELENS_VERSION", "--environment", ENVIRONMENT],
                  "Read deployed version").decode("utf-8").strip()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version) and version != "latest",
            "azd did not record a concrete deployed version")
    record(agent=SERVICE, version=version)
    deadline = time.monotonic() + 180
    while True:
        result = cli_json(["azd", "ai", "agent", "show", SERVICE, "--environment", ENVIRONMENT,
                           "--output", "json", "--no-prompt"], "Read deployed agent readiness")
        require(result.get("name") == SERVICE and result.get("version") == version,
                "Readiness result does not match the deployed agent/version")
        status = result.get("status")
        require(status not in {"failed", "stopped", "deleted"}, "Deployed agent is not healthy; inspect it in Foundry")
        if status == "active":
            break
        require(time.monotonic() < deadline, "Deployed agent did not become active before the deadline")
        time.sleep(5)
    endpoints = result.get("agent_endpoints", {})
    require(isinstance(endpoints, dict), "Readiness returned invalid protocol endpoints")
    target = read_state()["target"]
    for protocol, suffix in (("invocations", "invocations"), ("responses", "openai/responses")):
        endpoint = https_url(endpoints.get(protocol, ""), f"{protocol} endpoint")
        require(urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, "", ""))
                == f"{target}/agents/{SERVICE}/endpoint/protocols/{suffix}",
                "The deployed endpoint does not match the configured target")
        record(**{protocol: "attempted"})
        with tempfile.TemporaryDirectory(prefix="issuelens-smoke-", dir=scratch(".")) as directory:
            path = Path(directory) / "request.txt"
            # The pinned CLI wraps Responses file contents as input text, but
            # forwards Invocations files as the complete HTTP request body.
            payload = json.dumps({"input": SMOKE_INPUT})
            options = []
            if protocol == "responses":
                payload = SMOKE_INPUT
                options = ["--new-conversation"]
            path.write_text(payload, encoding="utf-8")
            raw = cli([
                "azd", "ai", "agent", "invoke", SERVICE, "--environment", ENVIRONMENT,
                "--protocol", protocol, "--version", version, "--new-session",
                "--input-file", str(path), "--timeout", "120", "--output", "raw", "--no-prompt", *options,
            ], f"{protocol} smoke request", timeout=150)
        response, body = http_response(raw)
        if protocol == "invocations":
            text = _action.read_response(response, NoTools())
            require(not response.read().strip(), "Invocations returned events after completion")
        else:
            text = responses_text(body)
        require(text.strip() == ACKNOWLEDGEMENT, f"{protocol} did not return the expected acknowledgement")
        record(**{protocol: "passed"})
        print(f"{protocol} smoke check passed for version {version}.")


def summary():
    state = read_state()
    lines = ["## IssueLens Foundry deployment", ""]
    for label, value in (
        ("Commit", state.get("commit", os.environ.get("GITHUB_SHA", "unavailable"))),
        ("Environment", ENVIRONMENT), ("Target", state.get("target", "not configured")),
        ("Agent version", state.get("version", "not recorded")),
        ("Code ZIP SHA-256", state.get("package_sha256", "not recorded")),
        ("Publication", state.get("publication", "not started")),
        ("Publish step", os.environ.get("PUBLISH_OUTCOME", "unknown")),
        ("Invocations", state.get("invocations", "not run")),
        ("Responses", state.get("responses", "not run")),
        ("Verify step", os.environ.get("VERIFY_OUTCOME", "unknown")),
    ):
        lines.append(f"- **{label}:** `{_action._display.safe_text(str(value)).replace('`', '')}`")
    if state.get("error"):
        lines.extend(["", _action._display.safe_text(state["error"])])
    if state.get("publication") in {"attempted", "completed"} and (
        os.environ.get("VERIFY_OUTCOME") != "success"
        or state.get("invocations") != "passed" or state.get("responses") != "passed"
    ):
        lines.extend(["", "**A version may already have been published.** Inspect Foundry before another "
                      "deployment. No automatic retry or rollback was performed."])
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def cleanup():
    state = read_state()
    if state.get("owns_azure_state") and (ROOT / ".azure").exists():
        shutil.rmtree(ROOT / ".azure")
    for name in ("issuelens-deployment-config.json", "issuelens-deployment.json"):
        scratch(name).unlink(missing_ok=True)
    directory = scratch("issuelens-code-package")
    if directory.exists():
        require(directory.is_dir() and not directory.is_symlink(), "Refusing unsafe package cleanup")
        shutil.rmtree(directory)


def main():
    commands = {
        "preflight": preflight, "prepare": prepare, "configure": configure,
        "package-check": package_check, "publish": publish, "verify": verify,
        "summary": summary, "cleanup": cleanup,
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=commands)
    command = parser.parse_args().command
    try:
        commands[command]()
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        # Only controlled ValueError messages are safe to publish; parser/OS
        # exceptions can include configuration values or raw remote content.
        message = str(error) if type(error) is ValueError else f"{command} failed: {type(error).__name__}"
        if command not in {"preflight", "summary", "cleanup"}:
            record(error=read_state().get("error") or message)
        print(f"::error::{_action._display.safe_text(message)}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
