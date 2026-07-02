"""
A2A Client demo — full protocol flow:
  1. Fetch agent card from AgentStack platform
  2. Send a message via A2A JSON-RPC
  3. Show streaming response token by token
"""

import asyncio
import json
import sys
import uuid

import httpx

PLATFORM_URL = "http://localhost:8333"
PROVIDER_ID = "d55c2378-bd90-407e-1335-ca4176b0eaa4"
AUTH_HEADER = "Basic YWRtaW46bXktc2VjcmV0LXBhc3N3b3Jk"

LLM_EXTENSION_URI = "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1"
LLM_EXTENSION_DATA = {
    LLM_EXTENSION_URI: {
        "llm_fulfillments": {
            "default": {
                "api_base": "{platform_url}/api/v1/openai",
                "api_key": "platform-agent-key",
                "api_model": "openai:llama-3.1-8b-instant",
            }
        }
    }
}

AGENT_BASE_URL = f"{PLATFORM_URL}/api/v1/a2a/{PROVIDER_ID}"
AGENT_CARD_URL = f"{AGENT_BASE_URL}/.well-known/agent-card.json"


async def fetch_agent_card(client: httpx.AsyncClient) -> dict:
    print("=" * 60)
    print("STEP 1 — Fetching agent card")
    print(f"  GET {AGENT_CARD_URL}")
    print("=" * 60)

    resp = await client.get(AGENT_CARD_URL)
    resp.raise_for_status()
    card = resp.json()

    print(f"  Name        : {card.get('name')}")
    print(f"  Description : {card.get('description', '').strip()[:80]}")
    print(f"  Version     : {card.get('version')}")
    print(f"  URL         : {card.get('url')}")
    print()

    extensions = card.get("capabilities", {}).get("extensions", [])
    print(f"  Extensions ({len(extensions)}):")
    for ext in extensions:
        req = "required" if ext.get("required") else "optional"
        print(f"    [{req}] {ext['uri']}")
    print()

    interfaces = card.get("additionalInterfaces", [])
    print(f"  Transports ({len(interfaces)}):")
    for iface in interfaces:
        print(f"    {iface['transport']} → {iface['url']}")
    print()

    return card


async def send_message(
    client: httpx.AsyncClient,
    text: str,
    context_id: str | None = None,
) -> str:
    msg_id = str(uuid.uuid4())
    context_id = context_id or str(uuid.uuid4())

    print("=" * 60)
    print("STEP 2 — Sending A2A message (JSON-RPC)")
    print(f"  POST {AGENT_BASE_URL}")
    print(f"  context_id  : {context_id}")
    print(f"  message     : {text!r}")
    print("=" * 60)

    payload = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": str(uuid.uuid4()),
        "params": {
            "message": {
                "messageId": msg_id,
                "contextId": context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": LLM_EXTENSION_DATA,
            }
        },
    }

    resp = await client.post(AGENT_BASE_URL, json=payload)
    resp.raise_for_status()
    result = resp.json()

    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return context_id

    task = result.get("result", {})
    status = task.get("status", {})
    history = task.get("history", [])

    print(f"  Task ID     : {task.get('id')}")
    print(f"  State       : {status.get('state')}")
    print()

    print("STEP 3 — Agent response (streaming tokens reassembled):")
    print("-" * 60)

    agent_msgs = [m for m in history if m.get("role") == "agent"]
    full_text = ""
    for msg in agent_msgs:
        for part in msg.get("parts", []):
            if part.get("kind") == "text":
                full_text += part["text"]

    print(f"  {full_text}")
    print()

    return context_id


async def main():
    user_input = sys.argv[1] if len(sys.argv) > 1 else "Hola! Quién eres y qué puedes hacer?"

    async with httpx.AsyncClient(
        headers={"Authorization": AUTH_HEADER},
        timeout=60.0,
    ) as client:
        # Step 1: agent card discovery
        await fetch_agent_card(client)

        # Step 2 & 3: send message and show response
        context_id = await send_message(client, user_input)

        # Step 4: multi-turn — send a follow-up in the same context
        print("=" * 60)
        print("STEP 4 — Multi-turn: follow-up in the same context")
        print("=" * 60)
        await send_message(client, "¿Cuánto es 2 + 2?", context_id=context_id)


if __name__ == "__main__":
    asyncio.run(main())
