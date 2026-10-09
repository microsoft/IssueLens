"""Standalone, single-attempt GitHub workflow dispatch transport."""

import json
import os
import re
import urllib.parse
import urllib.request


MAX_BYTES = 64 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "workflow-inputs contains duplicate keys")
        result[key] = value
    return result


def configuration():
    repository = os.environ.get("COORDINATOR_REPOSITORY", "")
    workflow = os.environ.get("COORDINATOR_WORKFLOW", "")
    reference = os.environ.get("COORDINATOR_REF", "")
    token = os.environ.get("DISPATCH_TOKEN", "")
    require(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
                         repository) and ".." not in repository, "Invalid coordinator-repository")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.ya?ml", workflow)
            and ".." not in workflow, "coordinator-workflow must be a YAML workflow basename")
    require(len(reference) <= 255
            and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
                    and not part.endswith((".", ".lock")) for part in reference.split("/"))
            and ".." not in reference and not reference.startswith("refs/"),
            "coordinator-ref must be a simple branch name")
    require(token.strip() and not any(character.isspace() for character in token),
            "dispatch-token must be non-empty and contain no whitespace")
    content = os.environ.get("WORKFLOW_INPUTS", "{}")
    require(len(content.encode("utf-8")) <= MAX_BYTES, "workflow-inputs exceeds 64 KiB")
    try:
        inputs = json.loads(content, object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        raise ValueError("workflow-inputs must be a JSON object") from None
    require(isinstance(inputs, dict) and len(inputs) <= 10, "workflow-inputs must contain at most ten inputs")
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key) and isinstance(value, str)
                for key, value in inputs.items()), "workflow-inputs requires valid names and string values")
    require(token not in content and token not in json.dumps(inputs),
            "workflow-inputs must not contain the dispatch credential")
    payload = json.dumps({"ref": reference, "inputs": inputs}).encode("utf-8")
    require(len(payload) <= MAX_BYTES, "Dispatch payload exceeds 64 KiB")
    return repository, workflow, reference, token, payload


def request(path, token, payload=None):
    return urllib.request.Request(
        "https://api.github.com" + path, data=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def read(opener, path, token):
    try:
        with opener.open(request(path, token), timeout=30) as response:
            status = response.status
            content = response.read(MAX_BYTES + 1)
    except Exception:
        raise ValueError("Target metadata validation failed; no workflow dispatch was sent") from None
    require(status == 200, "Target metadata was not acknowledged")
    require(len(content) <= MAX_BYTES, "Target metadata exceeds 64 KiB")
    # Invalid remote content must never become a user-facing diagnostic.
    try:
        result = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Invalid target metadata") from None
    require(isinstance(result, dict), "Invalid target metadata")
    return result


def dispatch():
    repository, workflow, reference, token, payload = configuration()
    opener = urllib.request.build_opener(NoRedirect())
    project = read(opener, f"/repos/{repository}", token)
    require(isinstance(project.get("full_name"), str)
            and project["full_name"].lower() == repository.lower()
            and type(project.get("id")) is int and project["id"] > 0,
            "Target repository identity mismatch")
    selected = read(opener, f"/repos/{repository}/actions/workflows/{workflow}", token)
    require(type(selected.get("id")) is int and selected["id"] > 0
            and selected.get("path") == ".github/workflows/" + workflow and selected.get("state") == "active",
            "Target workflow identity mismatch or workflow is inactive")
    branch = read(opener, f"/repos/{repository}/branches/{urllib.parse.quote(reference, safe='')}", token)
    require(branch.get("name") == reference and isinstance(branch.get("commit"), dict)
            and isinstance(branch["commit"].get("sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", branch["commit"]["sha"])
            and branch["commit"]["sha"] != "0" * 40, "Target branch identity mismatch")
    try:
        with opener.open(request(f"/repos/{repository}/actions/workflows/{workflow}/dispatches", token, payload),
                         timeout=30) as response:
            status = response.status
    except Exception:
        raise ValueError("Dispatch failed or its outcome is unknown; inspect target runs before retrying") from None
    require(status in {200, 204}, "Dispatch was not acknowledged; inspect target runs before retrying")
    try:
        print("Workflow dispatch accepted; target execution and job completion are not confirmed")
    except (OSError, ValueError):
        raise ValueError("Dispatch acknowledged but output unavailable; target job completion is unconfirmed") from None


def run():
    try:
        dispatch()
    except ValueError as error:
        raise SystemExit(f"::error::{error}") from None
    except Exception:
        raise SystemExit("::error::Target validation failed; no workflow dispatch was sent") from None


if __name__ == "__main__":
    run()
