"""
Translator agent — specialized A2A agent that translates text to a configured target language.

Runs independently from agent.py on a separate port (default: 8001).
Target language is controlled via the TARGET_LANGUAGE env var.

Usage:
    TARGET_LANGUAGE=French uv run --env-file .env.translator translator
"""

import os
from typing import Annotated

import httpx
from a2a.types import Message
from a2a.utils.message import get_message_text
from openai import AsyncOpenAI

from agentstack_sdk.a2a.extensions.services.llm import LLMServiceExtensionServer, LLMServiceExtensionSpec
from agentstack_sdk.platform.client import PlatformClient
from agentstack_sdk.server import Server
from agentstack_sdk.server.context import RunContext

server = Server()

TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "English")

_SYSTEM_PROMPT = (
    f"Translate the following text to {TARGET_LANGUAGE}. "
    f"Output ONLY the translation. No explanations, no notes, no original text."
)

@server.agent()
async def translator(
    input: Message,
    context: RunContext,
    llm: Annotated[LLMServiceExtensionServer, LLMServiceExtensionSpec.single_demand()],
):
    """Translates any text to the target language configured via TARGET_LANGUAGE env var."""

    if not llm.data or not llm.data.llm_fulfillments:
        yield "No LLM model configured. Please set up a model provider in Agent Stack."
        return

    fulfillment = next(iter(llm.data.llm_fulfillments.values()))
    client = AsyncOpenAI(base_url=fulfillment.api_base, api_key=fulfillment.api_key)

    stream = await client.chat.completions.create(
        model=fulfillment.api_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": get_message_text(input)},
        ],
        stream=True,
    )

    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


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
            port=int(os.getenv("PORT", 8001)),
            self_registration_client_factory=_make_registration_client,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
