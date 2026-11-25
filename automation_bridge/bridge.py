"""Authenticate workflow jobs and hold the target reservation through settlement."""

import hashlib
import http.client
import importlib.util
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableTransactionError, UpdateMode
from azure.identity import ManagedIdentityCredential

_action_path = Path(__file__).with_name("issuelens_action.py")
if not _action_path.is_file():
    _action_path = Path(__file__).parent.parent / ".github" / "actions" / "issuelens" / _action_path.name
_spec = importlib.util.spec_from_file_location("issuelens_bridge_action", _action_path)
assert _spec is not None and _spec.loader is not None
action = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(action)

PILOT_REPOSITORY = action.COORDINATOR_REPOSITORY
PILOT_BRANCH = "main"
JAVA_REPOSITORY = "microsoft/vscode-java-pack"
JAVA_REPOSITORY_ID = 104967329
JAVA_WORKFLOW = ".github/workflows/issueLens-run.yml"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
KEYS = jwt.PyJWKClient(GITHUB_ISSUER + "/.well-known/jwks", timeout=5)
JOB_ID = re.compile(r"[0-9a-f]{64}")
QUEUED, RUNNING, COMPLETED, FAILED, UNKNOWN = "queued", "running", "completed", "failed", "unknown"
PARTITION = "issuelens-pilot"
MAX_QUEUE_AGE_SECONDS = 600
MAX_REQUEST = 32 * 1024
MAX_RESULT = 60 * 1024
require = action.require


class BusyError(Exception):
    """A different job still owns the target reservation."""


def load_repository_allowlist(value: str, pilot_repository_id: int) -> frozenset[int]:
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 32 * 1024,
            "Invalid bridge repository allowlist size")
    ids = json.loads(value, object_pairs_hook=action.unique_object)
    require(isinstance(ids, list) and ids
            and all(type(identifier) is int and identifier > 0 for identifier in ids)
            and len(set(ids)) == len(ids)
            and type(pilot_repository_id) is int and pilot_repository_id != JAVA_REPOSITORY_ID
            and pilot_repository_id in ids and set(ids) <= {pilot_repository_id, JAVA_REPOSITORY_ID},
            "Invalid bridge repository ID allowlist")
    return frozenset(ids)


def workflow_claims(token: str, audience: str, pilot_repository_id: int, allowlist: frozenset[int]) -> dict:
    require(type(pilot_repository_id) is int and pilot_repository_id in allowlist,
            "Configured IssueLens repository ID does not match the bridge allowlist")
    require(len(token) <= 16 * 1024, "Invalid workflow token size")
    key = KEYS.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token, key.key, algorithms=["RS256"], issuer=GITHUB_ISSUER, audience=audience,
        options={"require": [
            "iss", "aud", "exp", "nbf", "iat", "repository", "repository_id", "repository_visibility",
            "ref", "workflow_ref", "workflow_sha", "event_name", "run_id", "run_attempt", "actor",
        ], "strict_aud": True},
    )
    require(isinstance(claims["repository"], str)
            and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", claims["repository"])
            and isinstance(claims["repository_id"], str)
            and claims["repository_id"].isdecimal()
            and int(claims["repository_id"]) in allowlist
            and (int(claims["repository_id"]) != pilot_repository_id
                 or claims["repository"] == PILOT_REPOSITORY)
            and (claims["repository"] != PILOT_REPOSITORY
                 or int(claims["repository_id"]) == pilot_repository_id)
            and ((claims["repository"] == JAVA_REPOSITORY)
                 == (int(claims["repository_id"]) == JAVA_REPOSITORY_ID))
            and claims["repository_visibility"] == "public"
            and isinstance(claims["ref"], str) and claims["ref"].startswith("refs/heads/")
            and claims.get("runner_environment") == "github-hosted",
            "Workflow is not in the IssueLens pilot scope")
    if "ref_protected" in claims:
        protected = claims["ref_protected"]
        require(protected is True or protected == "true", "Workflow ref was not protected")
    if claims["repository"] == PILOT_REPOSITORY:
        if claims["workflow_ref"] == workflow_ref(".github/workflows/issue-triage.yml"):
            require(claims["event_name"] in {"issues", "issue_comment", "workflow_dispatch"},
                    "Unsupported workflow event")
        elif claims["workflow_ref"] == workflow_ref(action.COORDINATOR_WORKFLOW):
            require(claims["event_name"] == "workflow_dispatch", "Unsupported workflow event")
            return _validated_claims(claims, "team-memory", int(claims["repository_id"]))
        else:
            raise ValueError("Workflow is not enabled for this bridge")
    else:
        require(claims["repository"] == JAVA_REPOSITORY and claims["ref"] == "refs/heads/main",
                "Workflow is not enabled for this bridge")
        if claims["workflow_ref"] == workflow_ref(action.COORDINATOR_WORKFLOW, JAVA_REPOSITORY):
            require(claims["event_name"] == "workflow_dispatch", "Unsupported workflow event")
            return _validated_claims(claims, "team-memory", int(claims["repository_id"]))
        require(claims["workflow_ref"] == workflow_ref(JAVA_WORKFLOW, JAVA_REPOSITORY)
                and claims["event_name"] in {"issues", "issue_comment", "workflow_dispatch"},
                "Workflow is not enabled for this bridge")
    return _validated_claims(claims, "issue-loop", int(claims["repository_id"]))


def _validated_claims(claims: dict, kind: str, repository_id: int) -> dict:
    return {
        **claims, "kind": kind, "repository_id": repository_id,
        "run_id": action.positive(claims["run_id"]), "run_attempt": action.positive(claims["run_attempt"]),
        "workflow_sha": action.full_sha(claims["workflow_sha"]),
    }


def workflow_ref(path: str, repository: str = PILOT_REPOSITORY, branch: str = PILOT_BRANCH) -> str:
    return f"{repository}/{path}@refs/heads/{branch}"


def default_branch_protected(claims: dict) -> bool:
    repository = claims["repository"]
    project = action.github_read(f"/repos/{repository}", token="", timeout=5)
    branch_name = project.get("default_branch")
    require(isinstance(project.get("full_name"), str) and type(project.get("id")) is int
            and type(project.get("private")) is bool and isinstance(project.get("visibility"), str)
            and isinstance(branch_name, str) and branch_name,
            "Invalid GitHub repository response")
    if (project["full_name"].casefold() != repository.casefold()
            or (repository == JAVA_REPOSITORY and project["full_name"] != JAVA_REPOSITORY)
            or project["id"] != claims["repository_id"] or project["private"]
            or project["visibility"] != "public" or claims["ref"] != f"refs/heads/{branch_name}"):
        return False
    if "ref_protected" in claims:
        return True
    branch_path = urllib.parse.quote(branch_name, safe="")
    branch = action.github_read(f"/repos/{repository}/branches/{branch_path}", token="", timeout=5)
    require(branch.get("name") == branch_name and isinstance(branch.get("protected"), bool),
            "Invalid GitHub branch protection response")
    return branch["protected"]


def job_id(claims: dict) -> str:
    return hashlib.sha256(
        f"v1:{claims['repository_id']}:{claims['kind']}:{claims['run_id']}".encode("ascii")
    ).hexdigest()


def _login(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}(?:\[bot\])?", value)


def _association(value):
    return value is None or isinstance(value, str) and value in {
        "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE", "MANNEQUIN",
    }


def _positive_int(value):
    require(type(value) is int, "Expected a numeric identifier")
    return action.positive(value)


def _issue_job(metadata: dict, claims: dict) -> str:
    event = claims["event_name"]
    operation = metadata.get("event_action")
    require(metadata.get("event_name") == event and operation in {
        "issues": ("opened", "reopened"),
        "issue_comment": ("created", "edited"),
        "workflow_dispatch": ("workflow_dispatch",),
    }[event], "Issue-event identity mismatch")
    comment_event = event == "issue_comment"
    created = comment_event and operation == "created"
    require(set(metadata) == {
        "repository", "issue_number", "event_name", "event_action", "actor_login", "actor_type",
        "issue_author_association", "comment_id", "comment_author_login", "comment_author_association",
        "comment_added", "comment_edited", "manual_dispatch",
    } | ({"comment_sha256"} if created else set()), "Unexpected issue-event fields")
    require(_login(metadata["actor_login"]) and metadata["actor_login"] == claims["actor"]
            and metadata["actor_type"] in ("User", "Bot", "Organization", "Mannequin")
            and _association(metadata["issue_author_association"])
            and _association(metadata["comment_author_association"])
            and metadata["comment_added"] is created
            and metadata["comment_edited"] is (comment_event and operation == "edited")
            and metadata["manual_dispatch"] is (event == "workflow_dispatch"), "Invalid issue-event metadata")
    if comment_event:
        require(metadata["actor_type"] == "User" and _login(metadata["comment_author_login"]),
                "Only human issue-comment events are allowed")
        _positive_int(metadata["comment_id"])
    else:
        require(all(metadata[field] is None for field in (
            "comment_id", "comment_author_login", "comment_author_association",
        )), "Unexpected comment metadata")
    if created:
        require(metadata["comment_author_login"] == metadata["actor_login"]
                and isinstance(metadata["comment_sha256"], str) and JOB_ID.fullmatch(metadata["comment_sha256"]),
                "Created comment requires its revision digest")
    return f"issue-{_positive_int(metadata['issue_number'])}"


_MERGE_FIELDS = {"pull_number", "merge_commit_sha", "merged_at", "source_identity"}


def _validate_merge(merge: dict, repository: str):
    number = _positive_int(merge.get("pull_number"))
    sha = action.full_sha(merge.get("merge_commit_sha"))
    merged_at = merge.get("merged_at")
    require(merge.get("source_identity") == f"{repository}#{number}:{sha}"
            and isinstance(merged_at, str) and re.fullmatch(r"[0-9T:Z.+-]{1,64}", merged_at),
            "Invalid merged PR identity")
    return number


def _memory_job(metadata: dict, claims: dict) -> str:
    repository = claims["repository"]
    require(metadata.get("repository_id") == claims["repository_id"]
            and metadata.get("base_ref") == PILOT_BRANCH
            and metadata.get("coordinator_repository") == repository
            and metadata.get("required_wiki_repository") == repository
            and metadata.get("coordinator_workflow_ref") == claims["workflow_ref"]
            and metadata.get("coordinator_workflow_sha") == claims["workflow_sha"]
            and metadata.get("coordinator_run_id") == claims["run_id"]
            and metadata.get("coordinator_run_attempt") == claims["run_attempt"],
            "Team-memory coordinator identity mismatch")
    fields = {
        "repository", "repository_id", "base_ref", "event_name", "event_action",
        "actor_login", "triggering_actor", "workflow_ref", "workflow_sha", "run_id", "run_attempt",
        "coordinator_repository", "coordinator_workflow_ref", "coordinator_workflow_sha",
        "coordinator_run_id", "coordinator_run_attempt", "required_wiki_repository",
    }
    require(_login(metadata.get("actor_login")) and _login(metadata.get("triggering_actor")),
            "Invalid team-memory actor metadata")
    if metadata.get("event_name") == "push":
        require(set(metadata) == fields | {
            "range_origin", "push_before", "push_after", "source_tip_sha", "commit_count",
            "pull_requests", "source_identity",
        }, "Unexpected team-memory range fields")
        before, after = (action.full_sha(metadata[field]) for field in ("push_before", "push_after"))
        action.full_sha(metadata["source_tip_sha"])
        require(before != after and metadata["range_origin"] == "authorized-reconciliation"
                and metadata["source_identity"] == f"{repository}@{before}..{after}"
                and metadata["event_action"] == "push" and metadata["workflow_sha"] == after
                and metadata["workflow_ref"] == workflow_ref(action.DISPATCH_WORKFLOW, repository)
                and _positive_int(metadata["commit_count"]) <= action.MAX_PUSH_COMMITS,
                "Invalid reconciliation range metadata")
        _positive_int(metadata["run_id"])
        _positive_int(metadata["run_attempt"])
        pulls = metadata["pull_requests"]
        require(isinstance(pulls, list) and 0 < len(pulls) <= action.MAX_BATCH_PRS
                and all(isinstance(pull, dict) and set(pull) == _MERGE_FIELDS for pull in pulls),
                "Invalid reconciliation PR inventory")
        require(len({_validate_merge(pull, repository) for pull in pulls}) == len(pulls), "Duplicate merged PRs")
    else:
        require(set(metadata) == fields | _MERGE_FIELDS and metadata.get("event_name") == "workflow_dispatch"
                and metadata.get("event_action") == "workflow_dispatch"
                and metadata.get("actor_login") == claims["actor"]
                and all(metadata.get(field) == claims[field] for field in (
                    "workflow_ref", "workflow_sha", "run_id", "run_attempt",
                )), "Invalid manual team-memory metadata")
        _validate_merge(metadata, repository)
    return "wiki"


def validate_request(data: dict, claims: dict) -> str:
    require(isinstance(data, dict) and set(data) == {"request_type", "metadata"}
            and data["request_type"] == claims["kind"] and isinstance(data["metadata"], dict)
            and data["metadata"].get("repository") == claims["repository"], "Invalid workflow job")
    validator = _issue_job if claims["kind"] == "issue-loop" else _memory_job
    target = validator(data["metadata"], claims)
    return target if claims["repository"] == PILOT_REPOSITORY else f"repo-{claims['repository_id']}-{target}"


class JobStore:
    def __init__(self, table: TableClient):
        self.table = table

    def get(self, identifier: str):
        try:
            return self.table.get_entity(PARTITION, identifier)
        except ResourceNotFoundError:
            return None

    def reserve(self, identifier: str, target: str, payload: bytes):
        existing = self.get(identifier)
        if existing is not None:
            return existing
        job = {
            "PartitionKey": PARTITION, "RowKey": identifier, "target": target,
            "created": int(time.time()), "state": QUEUED, "payload": payload,
        }
        try:
            self.table.submit_transaction([
                ("create", {"PartitionKey": PARTITION, "RowKey": f"lock-{target}", "job_id": identifier}),
                ("create", job),
            ])
        except TableTransactionError as error:
            if error.status_code != 409:
                raise
            existing = self.get(identifier)
            if existing is not None:
                return existing
            raise BusyError("The target has a pending or unresolved job") from None
        return job

    @staticmethod
    def _etag(job):
        return {"etag": job.metadata["etag"], "match_condition": MatchConditions.IfNotModified, "mode": UpdateMode.REPLACE}

    def claim(self, job) -> bool:
        try:
            self.table.update_entity({**job, "state": RUNNING}, **self._etag(job))
        except ResourceModifiedError:
            return False
        return True

    def settle(self, job, state: str, result: bytes | None = None):
        settled = {key: value for key, value in job.items() if key != "payload"}
        settled["state"] = state
        if result is not None:
            settled["result"] = result
        update = ("update", settled, self._etag(job))
        if state == UNKNOWN:
            self.table.submit_transaction([update])
        else:
            reservation = self.table.get_entity(PARTITION, f"lock-{job['target']}")
            require(reservation["job_id"] == job["RowKey"], "Bridge job does not own its target reservation")
            self.table.submit_transaction([
                update, ("delete", reservation, {
                    "etag": reservation.metadata["etag"], "match_condition": MatchConditions.IfNotModified,
                }),
            ])


def receipt(job) -> dict:
    value = {"job_id": job["RowKey"], "state": job["state"]}
    if "result" in job:
        value["result"] = json.loads(job["result"])
    return value


def _verify_created_comment(metadata: dict, digest: str) -> None:
    repository = metadata["repository"]
    require(repository in {PILOT_REPOSITORY, JAVA_REPOSITORY}, "Invalid comment repository")
    identifier = _positive_int(metadata["comment_id"])
    number = _positive_int(metadata["issue_number"])
    comment = action.github_read(f"/repos/{repository}/issues/comments/{identifier}", token="")
    issue_url = comment.get("issue_url")
    created = comment.get("created_at")
    body = comment.get("body")
    require(
        comment.get("id") == identifier
        and isinstance(issue_url, str)
        and issue_url.casefold() == f"https://api.github.com/repos/{repository}/issues/{number}".casefold()
        and isinstance(created, str) and created and created == comment.get("updated_at")
        and isinstance(body, str) and hashlib.sha256(body.encode("utf-8")).hexdigest() == digest,
        "GitHub comment no longer matches its created revision",
    )


class FoundryWorker:
    def __init__(self, url: str, credential: ManagedIdentityCredential):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == "https"
                and re.fullmatch(r"[a-z0-9-]+\.services\.ai\.azure\.com", parsed.netloc)
                and parsed.path.endswith("/protocols/invocations") and not parsed.fragment,
                "Invalid bridge Foundry endpoint")
        self.url = url
        self.credential = credential

    def prepare(self, job) -> urllib.request.Request:
        payload = json.loads(job["payload"])
        kind, metadata = payload["request_type"], payload["metadata"]
        if kind == "issue-loop":
            digest = metadata.pop("comment_sha256", None)
            if metadata["comment_added"]:
                require(isinstance(digest, str) and JOB_ID.fullmatch(digest), "Created comment requires its revision digest")
                _verify_created_comment(metadata, digest)
            invocation = action.build_issue_loop_request(metadata)
        elif kind == "team-memory":
            invocation = action.build_team_memory_request(metadata)
        else:
            raise ValueError("Unsupported bridge job type")
        token = self.credential.get_token("https://ai.azure.com/.default").token
        return urllib.request.Request(
            self.url, data=json.dumps(invocation).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST",
        )

    def run(self, store: JobStore, identifier: str) -> str | None:
        job = store.get(identifier)
        if job is None or job["state"] != QUEUED or not store.claim(job):
            return None
        job = store.get(identifier)
        if job is None or job["state"] != RUNNING:
            raise RuntimeError("Bridge job claim was lost")
        try:
            require(time.time() - job["created"] <= MAX_QUEUE_AGE_SECONDS, "Bridge queue deadline expired")
            request = self.prepare(job)
        except (AzureError, OSError, http.client.HTTPException, ValueError):
            store.settle(job, FAILED)
            return FAILED
        try:
            with urllib.request.build_opener(action.NoRedirect()).open(
                request, timeout=action.AGENT_STREAM_INACTIVITY_TIMEOUT_SECONDS,
            ) as response:
                text = action.read_response(response)
            payload = json.loads(job["payload"])
            result = None
            if payload["request_type"] == "team-memory":
                result = _memory_receipt(text, payload["metadata"])
        except urllib.error.HTTPError as error:
            if error.code in {400, 401, 403, 404, 422}:
                store.settle(job, FAILED)
                return FAILED
            store.settle(job, UNKNOWN)
            return UNKNOWN
        except (OSError, http.client.HTTPException, TypeError, ValueError):
            store.settle(job, UNKNOWN)
            return UNKNOWN
        store.settle(job, COMPLETED, result)
        return COMPLETED


def _memory_receipt(text: str, metadata: dict) -> bytes:
    parsed = json.loads(text, object_pairs_hook=action.unique_object)
    require(isinstance(parsed, dict), "Invalid maintenance receipt")
    action.validate_team_memory_result(parsed, metadata)
    result = {key: parsed[key] for key in ("source_repository", "status", "wiki_repository", "wiki_sha")}
    # Keep identifiers and status, not arbitrary model prose, in the durable store.
    result["reason"] = f"Team-memory outcome: {parsed['status']}."
    if metadata["event_name"] == "push":
        result.update(
            push_before=parsed["push_before"], push_after=parsed["push_after"],
            results=[{
                "pull_number": item["pull_number"], "merge_commit_sha": item["merge_commit_sha"],
                "status": item["status"], "reason": f"PR outcome: {item['status']}.",
            } for item in parsed["results"]],
        )
    else:
        result.update(pull_number=parsed["pull_number"], merge_commit_sha=parsed["merge_commit_sha"])
    encoded = json.dumps(result, separators=(",", ":")).encode("ascii")
    require(len(encoded) <= MAX_RESULT, "Maintenance receipt exceeds the storage budget")
    return encoded
