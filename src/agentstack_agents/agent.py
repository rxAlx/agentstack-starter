import os
from typing import Annotated

import httpx
from a2a.types import Message, Role
from a2a.utils.message import get_message_text
from openai import AsyncOpenAI

from agentstack_sdk.a2a.extensions.services.llm import LLMServiceExtensionServer, LLMServiceExtensionSpec
from agentstack_sdk.platform.client import PlatformClient
from agentstack_sdk.server import Server
from agentstack_sdk.server.context import RunContext

server = Server()

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")


@server.agent()
async def llm_agent(
    input: Message,
    context: RunContext,
    llm: Annotated[LLMServiceExtensionServer, LLMServiceExtensionSpec.single_demand()],
):
    """
    Basic LLM agent powered by the platform's configured model

    It uses the system prompt defined in the SYSTEM_PROMPT environment variable, or defaults to "You are a helpful assistant." if not set. The agent processes incoming messages, maintains conversation history, and streams responses from the LLM model in real-time.

    To set up an LLM model, it is required to configure a model provider via ubuntu CLI using the command:
    `
    curl -s -X POST http://localhost:8333/api/v1/model_providers \
    -H "Authorization: Basic YWRtaW46bXktc2VjcmV0LXBhc3N3b3Jk" \
    -H "Content-Type: application/json" \
    -d '{
        "name": "Gemini Flash",
        "type": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": "GOOGLE_STUDIO_API_KEY",
    }' | python3 -m json.tool
    `
    """

    if not llm.data or not llm.data.llm_fulfillments:
        yield "No LLM model configured. Please set up a model provider in Agent Stack."
        return

    fulfillment = next(iter(llm.data.llm_fulfillments.values()))
    client = AsyncOpenAI(base_url=fulfillment.api_base, api_key=fulfillment.api_key)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    async for msg in context.load_history():
        if isinstance(msg, Message):
            text = get_message_text(msg)
            if text:
                role = "assistant" if msg.role == Role.agent else "user"
                messages.append({"role": role, "content": text})

    messages.append({"role": "user", "content": get_message_text(input)})

    stream = await client.chat.completions.create(
        model=fulfillment.api_model,
        messages=messages,
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
    # When PLATFORM_URL is an internal k8s service URL, the Host header must match
    # the JWT audience (http://localhost:8333). PLATFORM_PUBLIC_HOST overrides it.
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
            port=int(os.getenv("PORT", 8000)),
            self_registration_client_factory=_make_registration_client,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
