import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PREV_AGENT_CARD_WELL_KNOWN_PATH,
)
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from clinical_decision_room.agent_executor import (
    FHIR_EXTENSION_URI,
    ClinicalDecisionExecutor,
)

JSONRPC_METHOD_ALIASES = {
    "Request": "message/send",
    "SendMessage": "message/send",
    "SendStreamingMessage": "message/stream",
    "message:send": "message/send",
    "message:stream": "message/stream",
    "tasks/send": "message/send",
    "tasks/sendSubscribe": "message/stream",
    "agent/authenticatedExtendedCard": "agent/getAuthenticatedExtendedCard",
    "tasks/pushNotification/set": "tasks/pushNotificationConfig/set",
    "tasks/pushNotification/get": "tasks/pushNotificationConfig/get",
}

LEGACY_SEND_METHODS = {"tasks/send", "tasks/sendSubscribe"}
SEND_METHODS = {
    "message/send",
    "message/stream",
    "Request",
    "SendMessage",
    "SendStreamingMessage",
    "message:send",
    "message:stream",
    "tasks/send",
    "tasks/sendSubscribe",
}
PROTO_STYLE_SEND_METHODS = {
    "Request",
    "SendMessage",
    "message:send",
}
ROLE_ALIASES = {
    "ROLE_USER": "user",
    "ROLE_AGENT": "agent",
}
RESPONSE_ROLE_ALIASES = {
    "user": "ROLE_USER",
    "agent": "ROLE_AGENT",
}
RESPONSE_STATE_ALIASES = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "canceled": "TASK_STATE_CANCELED",
    "failed": "TASK_STATE_FAILED",
    "rejected": "TASK_STATE_REJECTED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
    "unknown": "TASK_STATE_UNKNOWN",
}
LOGGER = logging.getLogger(__name__)


def load_dotenv() -> None:
    """Load local .env values without adding a runtime dependency."""
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def build_agent_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="Clinical Decision Room",
        description=(
            "AI-powered clinical decision support agent that analyzes patient data "
            "and clinical scenarios to provide differential diagnoses, recommended "
            "workups, management considerations, and red flag identification. "
            "Integrates with FHIR patient records when available."
        ),
        version="0.1.0",
        url=public_url,
        skills=[
            AgentSkill(
                id="clinical-decision-support",
                name="Clinical Decision Support",
                description=(
                    "Analyze clinical scenarios with optional FHIR patient data. "
                    "Provides differential diagnoses, diagnostic workup recommendations, "
                    "management options, and flags critical findings."
                ),
                tags=[
                    "clinical",
                    "diagnosis",
                    "decision-support",
                    "FHIR",
                    "healthcare",
                ],
                examples=[
                    "65-year-old male presenting with acute chest pain radiating to the left arm, diaphoresis, and shortness of breath. History of hypertension and diabetes.",
                    "Review this patient's recent lab results and medications for any concerning interactions or trends.",
                    "What is the differential diagnosis for a 30-year-old female with sudden onset severe headache and neck stiffness?",
                ],
            ),
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            extensions=[
                {
                    "uri": FHIR_EXTENSION_URI,
                    "description": "FHIR context allowing the agent to query a FHIR server securely for patient data",
                    "required": False,
                }
            ],
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


def build_compatible_agent_card_payload(agent_card: AgentCard) -> dict:
    """Return a card that includes both A2A v0.3 and v1 interface fields."""
    payload = agent_card.model_dump(exclude_none=True, by_alias=True)
    protocol_version = payload.get("protocolVersion", "0.3.0")
    interface = {
        "url": payload["url"],
        "transport": payload.get("preferredTransport", "JSONRPC"),
        "protocolBinding": payload.get("preferredTransport", "JSONRPC"),
        "protocolVersion": protocol_version,
    }

    payload.setdefault("supportedInterfaces", [interface])
    return payload


def _normalize_legacy_part(part: dict) -> dict:
    normalized = dict(part)
    if "type" in normalized and "kind" not in normalized:
        normalized["kind"] = normalized.pop("type")
    return normalized


def _normalize_message(message: dict, params: dict) -> dict:
    normalized_message = dict(message)
    task_id = params.get("id")
    session_id = params.get("sessionId")

    role = normalized_message.get("role")
    if role in ROLE_ALIASES:
        normalized_message["role"] = ROLE_ALIASES[role]

    if session_id and "contextId" not in normalized_message:
        normalized_message["contextId"] = session_id
    normalized_message.setdefault("messageId", f"{task_id or uuid4()}-message")

    parts = normalized_message.get("parts")
    if parts is None and isinstance(normalized_message.get("content"), list):
        parts = normalized_message.pop("content")
    if isinstance(parts, list):
        normalized_message["parts"] = [
            _normalize_legacy_part(part) if isinstance(part, dict) else part
            for part in parts
        ]

    return normalized_message


def normalize_jsonrpc_payload(payload: dict) -> dict:
    """Normalize legacy A2A JSON-RPC requests to the SDK's current schema."""
    normalized = dict(payload)
    original_method = normalized.get("method")
    if original_method in JSONRPC_METHOD_ALIASES:
        normalized["method"] = JSONRPC_METHOD_ALIASES[original_method]

    if original_method not in SEND_METHODS:
        return normalized

    params = normalized.get("params")
    if not isinstance(params, dict):
        return normalized

    message = params.get("message") or params.get("msg")
    if not isinstance(message, dict):
        return normalized

    normalized_params = {
        "message": _normalize_message(message, params),
    }
    if params.get("metadata") is not None:
        normalized_params["metadata"] = params["metadata"]

    configuration = {}
    if params.get("historyLength") is not None:
        configuration["historyLength"] = params["historyLength"]
    if params.get("pushNotification") is not None:
        configuration["pushNotificationConfig"] = params["pushNotification"]
    if configuration:
        normalized_params["configuration"] = configuration

    normalized["params"] = normalized_params
    return normalized


class JsonRpcMethodAliasMiddleware:
    """Rewrite legacy A2A JSON-RPC method names before SDK dispatch."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = b""
        original_method = None
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        rewritten_body = body
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                original_method = payload.get("method")
                print(
                    f"[a2a-debug] path={scope.get('path')} method={original_method!r}",
                    file=sys.stderr,
                    flush=True,
                )
                normalized_payload = normalize_jsonrpc_payload(payload)
                if normalized_payload != payload:
                    print(
                        "[a2a-debug] rewritten "
                        f"{original_method!r} -> {normalized_payload.get('method')!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                    LOGGER.info(
                        "Rewriting A2A JSON-RPC method %r -> %r",
                        original_method,
                        normalized_payload.get("method"),
                    )
                    rewritten_body = json.dumps(normalized_payload).encode("utf-8")
                else:
                    LOGGER.info(
                        "Received A2A JSON-RPC method %r",
                        original_method,
                    )
            else:
                print(
                    f"[a2a-debug] path={scope.get('path')} non-object JSON body",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception:
            print(
                f"[a2a-debug] path={scope.get('path')} non-JSON POST body",
                file=sys.stderr,
                flush=True,
            )
            rewritten_body = body

        async def replay_receive() -> dict:
            return {
                "type": "http.request",
                "body": rewritten_body,
                "more_body": False,
            }

        response_started = None
        response_body = b""

        async def capture_send(message: dict) -> None:
            nonlocal response_started, response_body
            if message["type"] == "http.response.start":
                response_started = dict(message)
                return

            if message["type"] != "http.response.body":
                await send(message)
                return

            response_body += message.get("body", b"")
            if message.get("more_body", False):
                return

            final_body = response_body
            if original_method in PROTO_STYLE_SEND_METHODS:
                final_body = _normalize_send_response_body(final_body)

            if response_started is not None:
                headers = [
                    (key, value)
                    for key, value in response_started.get("headers", [])
                    if key.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(final_body)).encode()))
                response_started["headers"] = headers
                await send(response_started)

            await send(
                {
                    "type": "http.response.body",
                    "body": final_body,
                    "more_body": False,
                }
            )

        await self.app(scope, replay_receive, capture_send)


def _normalize_send_response_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except Exception:
        return body

    if not isinstance(payload, dict) or "result" not in payload:
        return body

    result = payload["result"]
    if isinstance(result, dict) and result.get("kind") == "task":
        payload["result"] = {"task": _normalize_proto_response_value(result)}
        return json.dumps(payload).encode("utf-8")
    if isinstance(result, dict) and result.get("kind") == "message":
        payload["result"] = {"msg": _normalize_proto_response_value(result)}
        return json.dumps(payload).encode("utf-8")
    return body


def _normalize_proto_response_value(value):
    if isinstance(value, list):
        return [_normalize_proto_response_value(item) for item in value]

    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_proto_response_value(item)
        for key, item in value.items()
        if key != "kind"
    }

    role = normalized.get("role")
    if role in RESPONSE_ROLE_ALIASES:
        normalized["role"] = RESPONSE_ROLE_ALIASES[role]

    state = normalized.get("state")
    if state in RESPONSE_STATE_ALIASES:
        normalized["state"] = RESPONSE_STATE_ALIASES[state]

    return normalized


def build_app(public_url: str):
    agent_card = build_agent_card(public_url)
    executor = ClinicalDecisionExecutor()

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=handler,
    )

    app = a2a_app.build()

    async def compatible_agent_card(request):
        return JSONResponse(build_compatible_agent_card_payload(agent_card))

    # The Python A2A 0.3 SDK serves v0.3 card fields. Some clients already
    # require the v1 `supportedInterfaces` field, so serve a compatible superset.
    app.routes.insert(
        0,
        Route(
            AGENT_CARD_WELL_KNOWN_PATH,
            compatible_agent_card,
            methods=["GET"],
            name="compatible_agent_card",
        ),
    )
    app.routes.insert(
        1,
        Route(
            PREV_AGENT_CARD_WELL_KNOWN_PATH,
            compatible_agent_card,
            methods=["GET"],
            name="compatible_deprecated_agent_card",
        ),
    )
    app.routes.insert(
        2,
        Route(
            "/Request",
            a2a_app._handle_requests,
            methods=["POST"],
            name="compatible_a2a_request",
        ),
    )
    app.routes.insert(
        3,
        Route(
            "/request",
            a2a_app._handle_requests,
            methods=["POST"],
            name="compatible_a2a_request_lowercase",
        ),
    )

    app.add_middleware(JsonRpcMethodAliasMiddleware)
    return app


def main() -> None:
    load_dotenv()

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:9999")
    port = int(os.environ.get("PORT", "9999"))
    app = build_app(public_url)

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
