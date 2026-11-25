"""Azure Functions entry point. Deploy the same bundle as ingress and worker."""

import http.client
import json
import logging
import os
import re
from functools import lru_cache

import azure.functions as func
import jwt
from azure.core.exceptions import AzureError
from azure.data.tables import TableClient
from azure.identity import ManagedIdentityCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.servicebus.exceptions import ServiceBusError

import bridge

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def identity():
    return ManagedIdentityCredential()


@lru_cache(maxsize=1)
def jobs():
    storage = os.environ["ISSUELENS_BRIDGE_TABLE_ACCOUNT"]
    bridge.require(re.fullmatch(r"[a-z0-9]{3,24}", storage), "Invalid job storage account")
    client = TableClient(
        endpoint=f"https://{storage}.table.core.windows.net",
        table_name="IssueLensBridgeJobs", credential=identity(),
    )
    return bridge.JobStore(client)


@lru_cache(maxsize=1)
def service_bus():
    namespace = os.environ["ISSUELENS_BRIDGE_SERVICE_BUS__fullyQualifiedNamespace"]
    bridge.require(re.fullmatch(r"[a-z0-9-]+\.servicebus\.windows\.net", namespace), "Invalid job queue namespace")
    return ServiceBusClient(namespace, identity(), retry_total=0)


@lru_cache(maxsize=1)
def worker():
    return bridge.FoundryWorker(os.environ["ISSUELENS_BRIDGE_FOUNDRY_URL"], identity())


def respond(value, status=200):
    return func.HttpResponse(
        json.dumps(value, separators=(",", ":")), status_code=status, mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def authorized(request):
    auth = request.headers.get("Authorization", "")
    bridge.require(auth.startswith("Bearer ") and len(auth) > len("Bearer "),
                   "A workflow token is required")
    return bridge.workflow_claims(
        auth[len("Bearer "):], os.environ["ISSUELENS_BRIDGE_OIDC_AUDIENCE"],
        pilot_repository_id, repository_ids,
    )


role = os.environ.get("ISSUELENS_BRIDGE_ROLE")
if role == "ingress":
    pilot_repository_id = bridge.action.positive(os.environ["ISSUELENS_BRIDGE_REPOSITORY_ID"])
    repository_ids = bridge.load_repository_allowlist(
        os.environ.get("ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST", ""), pilot_repository_id,
    )

    @app.route(route="issuelens/v1/jobs", methods=["POST"], trigger_arg_name="request")
    def submit(request: func.HttpRequest) -> func.HttpResponse:
        try:
            claims = authorized(request)
        except jwt.PyJWKClientConnectionError:
            logger.warning("GitHub workflow signing keys unavailable")
            return respond({"error": "identity_provider_unavailable"}, 503)
        except (jwt.PyJWTError, ValueError):
            return respond({"error": "workflow_not_authorized"}, 401)
        try:
            body = request.get_body()
            bridge.require(0 < len(body) <= bridge.MAX_REQUEST, "Invalid job size")
            data = json.loads(body, object_pairs_hook=bridge.action.unique_object)
            target = bridge.validate_request(data, claims)
        except (ValueError, TypeError):
            return respond({"error": "invalid_job"}, 400)
        try:
            protected = bridge.default_branch_protected(claims)
        except (OSError, http.client.HTTPException, ValueError):
            logger.warning("GitHub repository or branch validation unavailable")
            return respond({"error": "branch_protection_unavailable"}, 503)
        if not protected:
            return respond({"error": "workflow_not_authorized"}, 401)
        identifier = bridge.job_id(claims)
        try:
            job = jobs().reserve(identifier, target, body, repository=claims["repository"])
            if job["state"] == bridge.QUEUED:
                # A repeated send can only reference the original reservation.
                with service_bus().get_queue_sender(os.environ["ISSUELENS_BRIDGE_QUEUE"]) as sender:
                    sender.send_messages(ServiceBusMessage(identifier, message_id=identifier), timeout=10)
            return respond(bridge.receipt(job), 202)
        except bridge.BusyError:
            return respond({"error": "target_has_unresolved_job"}, 409)
        except (AzureError, ServiceBusError):
            logger.warning("Bridge job admission not confirmed: %s", identifier)
            return respond({"error": "job_admission_not_confirmed", "job_id": identifier}, 503)

    @app.route(route="issuelens/v1/jobs/{job_id}", methods=["GET"], trigger_arg_name="request")
    def status(request: func.HttpRequest) -> func.HttpResponse:
        try:
            claims = authorized(request)
        except jwt.PyJWKClientConnectionError:
            logger.warning("GitHub workflow signing keys unavailable")
            return respond({"error": "identity_provider_unavailable"}, 503)
        except (jwt.PyJWTError, ValueError):
            return respond({"error": "workflow_not_authorized"}, 401)
        identifier = bridge.job_id(claims)
        if request.route_params.get("job_id") != identifier:
            return respond({"error": "job_not_found"}, 404)
        try:
            job = jobs().get(identifier)
        except AzureError:
            logger.warning("Bridge job status unavailable: %s", identifier)
            return respond({"error": "job_status_unavailable"}, 503)
        return respond(bridge.receipt(job)) if job is not None else respond({"error": "job_not_found"}, 404)
elif role == "worker":
    @app.service_bus_queue_trigger(
        arg_name="message", queue_name="%ISSUELENS_BRIDGE_QUEUE%", connection="ISSUELENS_BRIDGE_SERVICE_BUS",
    )
    def process(message: func.ServiceBusMessage) -> None:
        identifier = message.get_body().decode("ascii")
        if not bridge.JOB_ID.fullmatch(identifier):
            raise ValueError("Invalid queued bridge job ID")
        state = worker().run(jobs(), identifier)
        if state in {bridge.FAILED, bridge.UNKNOWN}:
            logger.warning("Bridge job %s: %s", identifier, state)
else:
    raise ValueError("Set ISSUELENS_BRIDGE_ROLE to ingress or worker")
