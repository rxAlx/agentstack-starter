"""
Orchestrator agent — demonstrates A2A agent-to-agent communication.

Flow:
  - Translation request detected → discover translator agent in the platform
                                 → call it via A2A JSON-RPC
                                 → stream the result back
  - Any other request           → answer directly with the LLM
"""

import base64
import os
import uuid
from typing import Annotated

import httpx
from a2a.types import Message
from a2a.utils.message import get_message_text
from openai import AsyncOpenAI

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

_TRANSLATION_KEYWORDS = ("translate", "traducir", "übersetz", "traduis", "traduci")

SYSTEM_PROMPT = "You are a helpful assistant. Answer concisely."


# ── helpers ──────────────────────────────────────────────────────────────────

def _needs_translation(text: str) -> bool:
    return any(kw in text.lower() for kw in _TRANSLATION_KEYWORDS)


async def _discover_translator(http_client: httpx.AsyncClient) -> str | None:
    """Query the platform registry to find the translator agent's A2A URL."""
    resp = await http_client.get(f"{PLATFORM_URL}/api/v1/providers")
    resp.raise_for_status()
    for provider in resp.json().get("items", []):
        card = provider.get("agent_card") or {}
        if card.get("name") == "translator" and provider.get("state") == "online":
            return f"{PLATFORM_URL}/api/v1/a2a/{provider['id']}"
    return None


async def _create_context_token(http_client: httpx.AsyncClient) -> str:
    """
    Mint a context token for the A2A hop to the translator.

    The platform proxy only fulfills the platform_api extension when the caller
    authenticates with a context token (Bearer) — with Basic auth the translator
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


async def _call_translator(text: str, api_base: str, api_key: str, model: str) -> str:
    """
    A2A agent-to-agent call:
      orchestrator → platform proxy → translator agent
    Passes the same LLM fulfillment the orchestrator received so the translator
    can call the same model provider without re-discovering credentials.
    """
    async with httpx.AsyncClient(
        headers=_platform_headers(),
        timeout=60.0,
    ) as http_client:

        translator_url = await _discover_translator(http_client)
        if not translator_url:
            return "[translator agent not found or offline]"

        context_token = await _create_context_token(http_client)

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
            translator_url,
            json=payload,
            headers={"Authorization": f"Bearer {context_token}"},
        )
        resp.raise_for_status()

        task = resp.json().get("result", {})
        translation = ""
        for msg in task.get("history", []):
            if msg.get("role") == "agent":
                for part in msg.get("parts", []):
                    if part.get("kind") == "text":
                        translation += part["text"]

        if not translation and task.get("status", {}).get("state") == "failed":
            status_msg = task.get("status", {}).get("message") or {}
            error_text = "".join(
                part.get("text", "")
                for part in status_msg.get("parts", [])
                if part.get("kind") == "text"
            )
            return f"[translator task failed: {error_text or 'unknown error'}]"

        return translation


# ── agent ────────────────────────────────────────────────────────────────────

@server.agent()
async def orchestrator(
    input: Message,
    context: RunContext,
    llm: Annotated[LLMServiceExtensionServer, LLMServiceExtensionSpec.single_demand()],
    _platform_api: Annotated[PlatformApiExtensionServer, PlatformApiExtensionSpec()],
):
    """
    Orchestrator agent. Delegates translation requests to the translator agent via A2A.
    Handles all other requests directly with the LLM.
    """

    if not llm.data or not llm.data.llm_fulfillments:
        yield "No LLM model configured."
        return

    fulfillment = next(iter(llm.data.llm_fulfillments.values()))
    text = get_message_text(input)

    if _needs_translation(text):
        yield "🔀 Delegating to **translator** agent via A2A...\n\n"
        translation = await _call_translator(text, fulfillment.api_base, fulfillment.api_key, fulfillment.api_model)
        yield translation

    else:
        client = AsyncOpenAI(base_url=fulfillment.api_base, api_key=fulfillment.api_key)
        stream = await client.chat.completions.create(
            model=fulfillment.api_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


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
            port=int(os.getenv("PORT", 8002)),
            self_registration_client_factory=_make_registration_client,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
