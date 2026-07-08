"""
Orchestrator v1 — planner/router agent that demonstrates the full A2A flow.

Unlike agent_orchestrator.py (keyword routing, single translator hop), this
version handles compound requests ("translate X and also answer Y"):

  1. PLAN      a small LLM call decomposes the user request into subtasks:
                 {"translate": <text to translate | null>,
                  "answer":    <question to answer  | null>}
  2. DISCOVER  GET /api/v1/providers on the platform registry to locate the
               agents needed for the plan (translator and/or llm_agent).
  3. DELEGATE  one A2A message/send per subtask through the platform proxy
               (POST /api/v1/a2a/{provider_id}), authenticated with a context
               token minted on the fly.

Every HTTP call of that flow is logged to stdout as "[A2A trace] ..." so the
protocol can be observed live with `kubectl logs deploy/orchestrator-v1 -n a2a`
(disable with A2A_TRACE=0).
"""

import base64
import json
import logging
import os
import uuid
from typing import Annotated

import httpx
from a2a.types import AgentSkill, Message
from a2a.utils.message import get_message_text
from openai import AsyncOpenAI

from agentstack_agents.card_security import SECURITY, SECURITY_SCHEMES
from agentstack_sdk.a2a.extensions.services.llm import LLMServiceExtensionServer, LLMServiceExtensionSpec
from agentstack_sdk.a2a.extensions.services.platform import PlatformApiExtensionServer, PlatformApiExtensionSpec
from agentstack_sdk.platform.client import PlatformClient
from agentstack_sdk.server import Server
from agentstack_sdk.server.context import RunContext

server = Server()

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://127.0.0.1:8333")

_admin_user = os.getenv("AGENTSTACK_ADMIN_USER", "admin")
_admin_password = os.getenv("AGENTSTACK_ADMIN_PASSWORD", "")
PLATFORM_AUTH = "Basic " + base64.b64encode(f"{_admin_user}:{_admin_password}".encode()).decode()

LLM_EXTENSION_URI = "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1"

_TRANSLATION_KEYWORDS = ("translate", "traducir", "traduce", "übersetz", "traduis", "traduci")


# ── HTTP trace ───────────────────────────────────────────────────────────────
# Logs every request/response of the A2A flow so the protocol can be watched
# live with `kubectl logs deploy/orchestrator-v1 -n a2a`.

_TRACE_ENABLED = os.getenv("A2A_TRACE", "1").lower() not in ("0", "false")

trace = logging.getLogger("a2a.trace")
if not trace.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[A2A trace] %(message)s"))
    trace.addHandler(_handler)
    trace.propagate = False
trace.setLevel(logging.INFO if _TRACE_ENABLED else logging.WARNING)


async def _trace_request(request: httpx.Request) -> None:
    trace.info("→ %s %s", request.method, request.url)


async def _trace_response(response: httpx.Response) -> None:
    trace.info("← %d %s %s", response.status_code, response.request.method, response.request.url)


_TRACE_HOOKS = {"request": [_trace_request], "response": [_trace_response]}


# ── 1. PLAN ──────────────────────────────────────────────────────────────────

_PLANNER_PROMPT = (
    "You are a task router. Split the user request into at most two subtasks. "
    "Copy text VERBATIM; never answer, rephrase or translate anything yourself.\n"
    "Return ONLY a JSON object:\n"
    '{"translate": <text to translate | null>, "question": <question to forward | null>}\n'
    "Rules:\n"
    '- "translate" only if the user EXPLICITLY asks to translate something; otherwise null.\n'
    '- "question" must keep the user\'s wording; never invent or rephrase it.\n'
    '- "question" must be self-contained: if it refers to other text in the request '
    '("what does it mean", "qué significa eso"), quote that text inside the question.\n'
    "Examples:\n"
    "User: Translate: 'good morning'. Also, what is DNS?\n"
    '{"translate": "good morning", "question": "what is DNS?"}\n'
    "User: Explícame por qué el cielo es azul\n"
    '{"translate": null, "question": "Explícame por qué el cielo es azul"}\n'
    "User: traduce 'break a leg' y dime qué significa\n"
    '{"translate": "break a leg", "question": "dime qué significa \'break a leg\'"}\n'
    "User: Traduce: 'hasta mañana'\n"
    '{"translate": "hasta mañana", "question": null}'
)


def _needs_translation(text: str) -> bool:
    return any(kw in text.lower() for kw in _TRANSLATION_KEYWORDS)


async def _plan(text: str, api_base: str, api_key: str, model: str) -> dict:
    """Decompose the request with the LLM; fall back to keyword routing."""
    try:
        async with httpx.AsyncClient(event_hooks=_TRACE_HOOKS) as planner_http:
            client = AsyncOpenAI(base_url=api_base, api_key=api_key, http_client=planner_http)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _PLANNER_PROMPT},
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
        raw = resp.choices[0].message.content
        parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        plan = {"translate": parsed.get("translate") or None, "answer": parsed.get("question") or None}
    except Exception as exc:
        trace.info("planner failed (%s); falling back to keyword routing", exc)
        plan = {"translate": text, "answer": None} if _needs_translation(text) else {"translate": None, "answer": text}

    # Guard: a small planner sometimes marks plain text as a translation subtask.
    # Only trust it when the user explicitly asked for a translation.
    if plan["translate"] and not _needs_translation(text):
        trace.info("guard: dropping translate subtask (no translation keyword in request)")
        plan["translate"] = None

    # Guard: each subtask travels alone, so a question like "dime qué significa"
    # loses its referent unless the referenced text travels with it. Only attach
    # it to referential questions — independent ones don't need the context.
    referential = ("significa", "significan", "means", "meaning", "refiere", "refers")
    if (
        plan["translate"]
        and plan["answer"]
        and plan["translate"] not in plan["answer"]
        and any(marker in plan["answer"].lower() for marker in referential)
    ):
        trace.info("guard: attaching referenced text to the question for context")
        plan["answer"] = f'{plan["answer"]} (the user request mentioned this text: "{plan["translate"]}")'

    if not plan["translate"] and not plan["answer"]:
        plan["answer"] = text
    trace.info("plan: translate=%r answer=%r", plan["translate"], plan["answer"])
    return plan


# ── 2. DISCOVER ──────────────────────────────────────────────────────────────

async def _discover_agents(http_client: httpx.AsyncClient, names: list[str]) -> dict[str, str]:
    """Query the platform registry and map agent name → A2A proxy URL."""
    resp = await http_client.get(f"{PLATFORM_URL}/api/v1/providers")
    resp.raise_for_status()
    urls: dict[str, str] = {}
    for provider in resp.json().get("items", []):
        card = provider.get("agent_card") or {}
        if card.get("name") in names and provider.get("state") == "online":
            urls[card["name"]] = f"{PLATFORM_URL}/api/v1/a2a/{provider['id']}"
    return urls


async def _create_context_token(http_client: httpx.AsyncClient) -> str:
    """
    Mint a context token for the A2A hops.

    The platform proxy only fulfills the platform_api extension when the caller
    authenticates with a context token (Bearer) — with Basic auth the callee
    fails with "Platform extension metadata was not provided".
    """
    resp = await http_client.post(f"{PLATFORM_URL}/api/v1/contexts", json={})
    resp.raise_for_status()
    context_id = resp.json()["id"]

    resp = await http_client.post(
        f"{PLATFORM_URL}/api/v1/contexts/{context_id}/token",
        json={
            "grant_global_permissions": {"llm": ["*"], "a2a_proxy": ["*"]},
            "grant_context_permissions": {
                "files": ["*"],
                "vector_stores": ["*"],
                "context_data": ["*"],
            },
        },
    )
    resp.raise_for_status()
    return resp.json()["token"]


# ── 3. DELEGATE ──────────────────────────────────────────────────────────────

def _llm_ext_metadata(api_base: str, api_key: str, model: str) -> dict:
    return {
        LLM_EXTENSION_URI: {
            "llm_fulfillments": {
                "default": {
                    "api_base": api_base,
                    "api_key": api_key,
                    "api_model": model,
                }
            }
        }
    }


def _platform_headers() -> dict:
    base = {"Authorization": PLATFORM_AUTH}
    public_host = os.getenv("PLATFORM_PUBLIC_HOST")
    if public_host:
        base["Host"] = public_host
    return base


async def _call_agent(
    http_client: httpx.AsyncClient,
    agent_url: str,
    text: str,
    context_token: str,
    api_base: str,
    api_key: str,
    model: str,
) -> str:
    """
    A2A agent-to-agent call:
      orchestrator_v1 → platform proxy → callee agent
    Passes the same LLM fulfillment the orchestrator received so the callee
    can call the same model provider without re-discovering credentials.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": str(uuid.uuid4()),
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": _llm_ext_metadata(api_base, api_key, model),
            }
        },
    }

    resp = await http_client.post(
        agent_url,
        json=payload,
        headers={"Authorization": f"Bearer {context_token}"},
    )
    resp.raise_for_status()

    task = resp.json().get("result", {})
    answer = ""
    for msg in task.get("history", []):
        if msg.get("role") == "agent":
            for part in msg.get("parts", []):
                if part.get("kind") == "text":
                    answer += part["text"]

    if not answer and task.get("status", {}).get("state") == "failed":
        status_msg = task.get("status", {}).get("message") or {}
        error_text = "".join(
            part.get("text", "")
            for part in status_msg.get("parts", [])
            if part.get("kind") == "text"
        )
        return f"[delegated task failed: {error_text or 'unknown error'}]"

    return answer


# ── agent ────────────────────────────────────────────────────────────────────

_SKILLS = [
    AgentSkill(
        id="delegate-translation",
        name="Delegate translation via A2A",
        description=(
            "Delegates the translation subtask to the translator agent through the "
            "platform A2A proxy (registry discovery + message/send with a context token)."
        ),
        tags=["orchestration", "a2a", "delegation", "translation"],
        examples=["Translate: Good morning, my friend!"],
        input_modes=["text"],
        output_modes=["text"],
    ),
    AgentSkill(
        id="delegate-answer",
        name="Delegate answers via A2A",
        description="Delegates question-answering subtasks to the llm_agent agent through the platform A2A proxy.",
        tags=["orchestration", "a2a", "delegation", "chat"],
        examples=["What is the A2A protocol?"],
        input_modes=["text"],
        output_modes=["text"],
    ),
]


@server.agent(
    security_schemes=SECURITY_SCHEMES,
    security=SECURITY,
    skills=_SKILLS,
)
async def orchestrator_v1(
    input: Message,
    context: RunContext,
    llm: Annotated[LLMServiceExtensionServer, LLMServiceExtensionSpec.single_demand()],
    _platform_api: Annotated[PlatformApiExtensionServer, PlatformApiExtensionSpec()],
):
    """
    Planner/router agent. Decomposes the user request with an LLM, then delegates
    each subtask to a specialized agent via A2A: translations go to the translator
    agent, questions go to the llm_agent agent. Handles compound requests
    (translation + question) with two A2A hops.
    """

    if not llm.data or not llm.data.llm_fulfillments:
        yield "No LLM model configured."
        return

    fulfillment = next(iter(llm.data.llm_fulfillments.values()))
    text = get_message_text(input)

    plan = await _plan(text, fulfillment.api_base, fulfillment.api_key, fulfillment.api_model)
    subtasks = [
        ("translator", plan["translate"], "🔀 Delegating to **translator** agent via A2A...\n\n"),
        ("llm_agent", plan["answer"], "🔀 Delegating to **llm_agent** agent via A2A...\n\n"),
    ]
    needed = [agent_name for agent_name, subtask, _ in subtasks if subtask]

    async with httpx.AsyncClient(
        headers=_platform_headers(),
        timeout=60.0,
        event_hooks=_TRACE_HOOKS,
    ) as http_client:

        agents = await _discover_agents(http_client, needed)
        context_token = await _create_context_token(http_client)

        first = True
        for agent_name, subtask, banner in subtasks:
            if not subtask:
                continue
            if not first:
                yield "\n\n"
            first = False

            yield banner
            if agent_name not in agents:
                yield f"[{agent_name} agent not found or offline]"
                continue
            yield await _call_agent(
                http_client,
                agents[agent_name],
                subtask,
                context_token,
                fulfillment.api_base,
                fulfillment.api_key,
                fulfillment.api_model,
            )


# ── server entry point ───────────────────────────────────────────────────────

def _make_registration_client() -> PlatformClient:
    platform_url = os.getenv("PLATFORM_URL", "http://127.0.0.1:8333")
    admin_user = os.getenv("AGENTSTACK_ADMIN_USER", "admin")
    admin_password = os.getenv("AGENTSTACK_ADMIN_PASSWORD", "")
    public_host = os.getenv("PLATFORM_PUBLIC_HOST")
    extra_headers = {"Host": public_host} if public_host else {}
    if admin_password:
        return PlatformClient(base_url=platform_url, auth=httpx.BasicAuth(admin_user, admin_password), headers=extra_headers)
    return PlatformClient(base_url=platform_url, headers=extra_headers)


def run():
    try:
        server.run(
            url=os.getenv("SERVER_URL"),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", 8003)),
            self_registration_client_factory=_make_registration_client,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
