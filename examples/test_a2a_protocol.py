"""
A2A protocol walkthrough — exercises every core A2A concept against the
agents deployed in Agent Stack (llm_agent, orchestrator, translator).

Phases:
  0. Platform setup (AgentStack-specific, NOT part of A2A):
       create context + context token, resolve an LLM model.
  1. Agent discovery via the platform registry (AgentStack-specific).
  2. A2A Agent Card discovery (A2A spec §5: /.well-known/agent-card.json).
  3. message/send  — JSON-RPC request/response, Task object lifecycle (§7.1).
  4. message/stream — Server-Sent Events: Task + TaskStatusUpdateEvent (§7.2).
  5. tasks/get     — retrieve a persisted Task by id (§7.3).
  6. Agent-to-agent orchestration: orchestrator delegates to translator via A2A.

Usage:
    kubectl port-forward -n a2a svc/agentstack-server-svc 8333:8333 &
    export AGENTSTACK_ADMIN_PASSWORD=$(kubectl get secret agentstack-agents-secret \
        -n a2a -o jsonpath='{.data.admin-password}' | base64 -d)
    uv run python examples/test_a2a_protocol.py
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid

import httpx

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8333")
ADMIN_USER = os.getenv("AGENTSTACK_ADMIN_USER", "admin")

# The agent pods call the LLM gateway themselves, so this URL must be
# resolvable from INSIDE the cluster (k8s service DNS), not from your laptop.
LLM_API_BASE_INTERNAL = os.getenv(
    "LLM_API_BASE_INTERNAL", "http://agentstack-server-svc:8333/api/v1/openai"
)

LLM_EXTENSION_URI = "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1"


def _admin_password() -> str:
    pw = os.getenv("AGENTSTACK_ADMIN_PASSWORD")
    if pw:
        return pw
    # Fallback: read it from the cluster secret
    out = subprocess.run(
        ["kubectl", "get", "secret", "agentstack-agents-secret", "-n", "a2a",
         "-o", "jsonpath={.data.admin-password}"],
        capture_output=True, text=True, check=True,
    )
    import base64
    return base64.b64decode(out.stdout).decode()


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def show_json(label: str, obj: dict, max_lines: int = 40) -> None:
    lines = json.dumps(obj, indent=2, ensure_ascii=False).splitlines()
    print(f"--- {label} ---")
    for line in lines[:max_lines]:
        print(f"  {line}")
    if len(lines) > max_lines:
        print(f"  ... ({len(lines) - max_lines} more lines)")


def jsonrpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def user_message(text: str, llm_metadata: dict | None = None, task_id: str | None = None) -> dict:
    msg: dict = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "kind": "message",
        "parts": [{"kind": "text", "text": text}],
    }
    if llm_metadata:
        msg["metadata"] = llm_metadata
    if task_id:
        msg["taskId"] = task_id
    return msg


def collect_agent_text(task: dict) -> str:
    """Reassemble the agent's answer from the Task history (one message per stream chunk)."""
    return "".join(
        part["text"]
        for msg in task.get("history", [])
        if msg.get("role") == "agent"
        for part in msg.get("parts", [])
        if part.get("kind") == "text"
    )


# ─── Phase 0: platform setup (AgentStack-specific, not A2A) ──────────────────

async def platform_setup(admin: httpx.AsyncClient) -> tuple[str, str, dict]:
    banner("PHASE 0 — Platform setup (AgentStack: context + token + LLM model)")
    print("NOTE: nothing in this phase is part of the A2A spec — this is the")
    print("platform's own security/service layer (the 'space' around the agents).")

    resp = await admin.post(f"{PLATFORM_URL}/api/v1/contexts", json={})
    resp.raise_for_status()
    context_id = resp.json()["id"]
    print(f"\n  POST /api/v1/contexts            -> context_id = {context_id}")

    resp = await admin.post(
        f"{PLATFORM_URL}/api/v1/contexts/{context_id}/token",
        json={
            "grant_global_permissions": {"llm": ["*"], "a2a_proxy": ["*"]},
            "grant_context_permissions": {
                "files": ["*"], "vector_stores": ["*"], "context_data": ["*"],
            },
        },
    )
    resp.raise_for_status()
    token = resp.json()["token"]
    print(f"  POST /api/v1/contexts/../token   -> context token ({len(token)} chars)")

    resp = await admin.post(
        f"{PLATFORM_URL}/api/v1/model_providers/match",
        json={"capability": "llm", "suggested_models": []},
    )
    resp.raise_for_status()
    model_id = resp.json()["items"][0]["model_id"]
    print(f"  POST /api/v1/model_providers/match -> model = {model_id}")

    # This is what the agent receives through the LLM service extension:
    # an OpenAI-compatible endpoint served by the platform, scoped by our token.
    llm_metadata = {
        LLM_EXTENSION_URI: {
            "llm_fulfillments": {
                "default": {
                    "api_base": LLM_API_BASE_INTERNAL,
                    "api_key": token,
                    "api_model": model_id,
                }
            }
        }
    }
    print(f"  LLM fulfillment api_base (in-cluster) = {LLM_API_BASE_INTERNAL}")
    return context_id, token, llm_metadata


# ─── Phase 1: registry discovery (AgentStack-specific) ───────────────────────

async def registry_discovery(admin: httpx.AsyncClient) -> dict[str, dict]:
    banner("PHASE 1 — Agent discovery via platform registry (GET /api/v1/providers)")
    print("This is the platform's catalog. In a data-space analogy this is the")
    print("'federated catalog'; A2A itself only standardizes the Agent Card (phase 2).")

    resp = await admin.get(f"{PLATFORM_URL}/api/v1/providers")
    resp.raise_for_status()
    agents: dict[str, dict] = {}
    print(f"\n  {'name':<24} {'state':<9} provider_id")
    for p in resp.json().get("items", []):
        card = p.get("agent_card") or {}
        name, state = card.get("name"), p.get("state")
        print(f"  {str(name):<24} {str(state):<9} {p['id']}")
        if state == "online" and name not in agents:
            agents[name] = {"id": p["id"], "url": f"{PLATFORM_URL}/api/v1/a2a/{p['id']}/"}
    missing = {"llm_agent", "orchestrator", "translator"} - agents.keys()
    if missing:
        sys.exit(f"\nERROR: agents not online: {missing}")
    return agents


# ─── Phase 2: A2A Agent Card discovery ───────────────────────────────────────

async def agent_card_discovery(client: httpx.AsyncClient, agents: dict[str, dict]) -> None:
    banner("PHASE 2 — A2A Agent Card discovery (/.well-known/agent-card.json)")
    print("THIS is pure A2A: every agent publishes a card describing identity,")
    print("transports, capabilities, skills and security requirements.")

    for name, info in agents.items():
        resp = await client.get(f"{info['url']}.well-known/agent-card.json")
        resp.raise_for_status()
        card = resp.json()
        caps = card.get("capabilities", {})
        exts = caps.get("extensions", [])
        print(f"\n  ── {name} " + "─" * (60 - len(name)))
        print(f"  protocolVersion : {card.get('protocolVersion')}")
        print(f"  url             : {card.get('url')}")
        print(f"  preferredTransport: {card.get('preferredTransport')}")
        print(f"  streaming       : {caps.get('streaming')}")
        print(f"  input/output    : {card.get('defaultInputModes')} / {card.get('defaultOutputModes')}")
        for s in card.get("skills", []):
            print(f"  skill           : {s.get('id')} — {str(s.get('description', ''))[:60]}")
        print(f"  extensions      : {len(exts)} declared:")
        for e in exts:
            print(f"      [{'required' if e.get('required') else 'optional'}] {e['uri']}")


# ─── Phase 3: message/send ───────────────────────────────────────────────────

async def send_to_translator(client: httpx.AsyncClient, agents: dict, llm_metadata: dict) -> str:
    banner("PHASE 3 — message/send to translator (JSON-RPC, Task lifecycle)")
    payload = jsonrpc("message/send", {
        "message": user_message("Good morning! The A2A protocol connects agents across platforms.", llm_metadata),
    })
    redacted = json.loads(json.dumps(payload))
    redacted["params"]["message"]["metadata"][LLM_EXTENSION_URI]["llm_fulfillments"]["default"]["api_key"] = "<context-token>"
    show_json("JSON-RPC request", redacted)

    resp = await client.post(agents["translator"]["url"], json=payload, timeout=120)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        show_json("JSON-RPC ERROR", body["error"])
        sys.exit(1)
    task = body["result"]
    print(f"\n  result.kind        = {task['kind']}   (the agent answered with a Task object)")
    print(f"  task.id            = {task['id']}")
    print(f"  task.contextId     = {task.get('contextId')}")
    print(f"  task.status.state  = {task['status']['state']}")
    print(f"  history            = {len(task.get('history', []))} messages "
          f"(1 user + N agent chunks: each stream token becomes a history entry)")
    print(f"\n  Reassembled answer: {collect_agent_text(task)!r}")
    return task["id"]


# ─── Phase 4: message/stream (SSE) ───────────────────────────────────────────

async def stream_from_llm_agent(client: httpx.AsyncClient, agents: dict, llm_metadata: dict) -> None:
    banner("PHASE 4 — message/stream to llm_agent (Server-Sent Events)")
    print("Same JSON-RPC envelope, but the response is an SSE stream of events:")
    print("Task (submitted) -> TaskStatusUpdateEvent(working, message chunks) -> final.")

    payload = jsonrpc("message/stream", {
        "message": user_message("In one short sentence: what is an agent card?", llm_metadata),
    })
    events, chunks = 0, []
    async with client.stream(
        "POST", agents["llm_agent"]["url"], json=payload,
        headers={"Accept": "text/event-stream"}, timeout=120,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            events += 1
            event = json.loads(line[len("data:"):]).get("result", {})
            kind = event.get("kind")
            if kind == "task":
                print(f"  [event {events:>2}] kind=task            state={event['status']['state']}  id={event['id']}")
            elif kind == "status-update":
                state = event["status"]["state"]
                msg = event["status"].get("message") or {}
                text = "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("kind") == "text")
                if text:
                    chunks.append(text)
                final = "  final=True" if event.get("final") else ""
                shown = f"  chunk={text!r}" if text else ""
                print(f"  [event {events:>2}] kind=status-update   state={state}{final}{shown}")
            else:
                print(f"  [event {events:>2}] kind={kind}")
    print(f"\n  {events} SSE events. Reassembled answer: {''.join(chunks)!r}")


# ─── Phase 5: tasks/get ──────────────────────────────────────────────────────

async def get_task(client: httpx.AsyncClient, agents: dict, task_id: str) -> None:
    banner("PHASE 5 — tasks/get (retrieve a persisted Task by id)")
    payload = jsonrpc("tasks/get", {"id": task_id, "historyLength": 3})
    show_json("JSON-RPC request", payload)
    resp = await client.post(agents["translator"]["url"], json=payload, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        show_json("JSON-RPC ERROR", body["error"])
        return
    task = body["result"]
    print(f"\n  The Task survives after completion — any authorized client can audit it:")
    print(f"  task.id={task['id']}  state={task['status']['state']}  "
          f"history(last 3)={len(task.get('history', []))} messages")


# ─── Phase 6: agent-to-agent orchestration ───────────────────────────────────

async def orchestrator_delegation(client: httpx.AsyncClient, agents: dict, llm_metadata: dict) -> None:
    banner("PHASE 6 — A2A agent-to-agent: orchestrator delegates to translator")
    print("We call the orchestrator; it discovers the translator in the registry")
    print("and calls it with its own A2A message/send (see agent_orchestrator.py).")

    payload = jsonrpc("message/send", {
        "message": user_message("Please translate: The stars look wonderful tonight.", llm_metadata),
    })
    resp = await client.post(agents["orchestrator"]["url"], json=payload, timeout=180)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        show_json("JSON-RPC ERROR", body["error"])
        return
    task = body["result"]
    answer = collect_agent_text(task)
    print(f"\n  task.status.state = {task['status']['state']}")
    print(f"  Orchestrator answer:\n    {answer}")
    if "Delegating" in answer:
        print("\n  ✔ The answer contains the delegation marker: the translation you see")
        print("    was produced by the translator agent, reached via a second A2A hop.")


async def main() -> None:
    password = _admin_password()
    async with httpx.AsyncClient(auth=(ADMIN_USER, password), timeout=60) as admin:
        context_id, token, llm_metadata = await platform_setup(admin)
        agents = await registry_discovery(admin)

        # From here on we act as a regular A2A client authenticated with the
        # context token (Bearer) — the platform proxy fulfills the platform_api
        # extension for us because the token carries the context.
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=60
        ) as client:
            await agent_card_discovery(client, agents)
            task_id = await send_to_translator(client, agents, llm_metadata)
            await stream_from_llm_agent(client, agents, llm_metadata)
            await get_task(client, agents, task_id)
            await orchestrator_delegation(client, agents, llm_metadata)

    banner("DONE — full A2A protocol exercised: discovery, send, stream, tasks, A2A hop")


if __name__ == "__main__":
    asyncio.run(main())
