#!/usr/bin/env python3
"""
Orchestrator v1 with ANP + DID WBA Integration
-----------------------------------------------
Upgrades the original orchestrator to use:
- ANP Registry for agent discovery (decentralized)
- DID WBA for authentication (decentralized identity)
- A2A protocol for task execution (preserved)

Flow:
1. PLAN: LLM decomposes user request
2. DISCOVER: Query ANP Registry by skill → get DID + AD + A2A URL
3. AUTHENTICATE: DID WBA to target agent's sidecar
4. DELEGATE: Send A2A task via proxy
"""

import base64
import json
import logging
import os
import uuid 
from typing import Annotated, Optional, Dict, Any

import httpx
from a2a.types import AgentSkill, Message,SecurityScheme, HTTPAuthSecurityScheme
from a2a.utils.message import get_message_text
from openai import AsyncOpenAI

from agentstack_sdk.a2a.extensions.services.llm import LLMServiceExtensionServer, LLMServiceExtensionSpec
from agentstack_sdk.a2a.extensions.services.platform import PlatformApiExtensionServer, PlatformApiExtensionSpec
from agentstack_sdk.platform.client import PlatformClient
from agentstack_sdk.server import Server
from agentstack_sdk.server.context import RunContext

# ANP imports
from anp.authentication import DIDWbaAuthHeader

server = Server()

# ── Configuration ────────────────────────────────────────────────────────────

ANP_REGISTRY_URL = os.getenv("ANP_REGISTRY_URL", "http://anp-registry:8080")
ORCHESTRATOR_DID_PATH = os.getenv("ORCHESTRATOR_DID_PATH", "/app/did/did.json")
ORCHESTRATOR_KEY_PATH = os.getenv("ORCHESTRATOR_KEY_PATH", "/app/did/private-key.pem")

# Fallback to platform for LLM service (still needed for planning)
PLATFORM_URL = os.getenv("PLATFORM_URL", "http://127.0.0.1:8333")
_admin_user = os.getenv("AGENTSTACK_ADMIN_USER", "admin")
_admin_password = os.getenv("AGENTSTACK_ADMIN_PASSWORD", "")
PLATFORM_AUTH = "Basic " + base64.b64encode(f"{_admin_user}:{_admin_password}".encode()).decode()

LLM_EXTENSION_URI = "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1"

_TRANSLATION_KEYWORDS = ("translate", "traducir", "traduce", "übersetz", "traduis", "traduci")

# AGENT CARD SECURITY SCHEMES (ADDED ANP DID WBA SCHEME)

SECURITY_SCHEMES = {
    "didWba": SecurityScheme(
        root=HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="JWT",
            description="DID WBA authentication via ANP sidecar",
        )
    ),
}

SECURITY = [{"didWba": []}]

# ── HTTP trace ───────────────────────────────────────────────────────────────

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


# ── ANP Discovery Client ─────────────────────────────────────────────────────

class ANPDiscoveryClient:
    """Client for discovering agents via ANP Registry."""
    
    def __init__(self, registry_url: str):
        self.registry_url = registry_url
    
    async def find_agent(self, skill: str) -> Optional[Dict[str, Any]]:
        """Find an agent by skill from ANP Registry."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.registry_url}/search",
                params={"skills": skill}
            )
            if resp.status_code != 200:
                trace.info("ANP registry query failed: %d", resp.status_code)
                return None
            
            results = resp.json()
            if results.get("count", 0) == 0:
                trace.info("No agent found with skill: %s", skill)
                return None
            
            agent = results["agents"][0]
            trace.info("ANP discovered: %s (%s)", agent["name"], agent["did"])
            return agent
    
    def _convert_to_proxy(self, url: str) -> Optional[str]:
        """Convert any agent URL to sidecar proxy URL."""
        import re
        
        match = re.match(r'http://([^/:]+)', url)
        if not match:
            return None
        
        hostname = match.group(1)
        parts = hostname.split('.')
        service_name = parts[0]
        
        # Ensure -svc suffix
        if not service_name.endswith('-svc'):
            parts[0] = f"{service_name}-svc"
            hostname = '.'.join(parts)
        
        # If hostname has no dots, add full cluster domain
        if '.' not in hostname:
            hostname = f"{hostname}.a2a.svc.cluster.local"
        
        return f"http://{hostname}:8001/a2a-proxy/.well-known/agent-card.json"


# ── DID WBA Authentication ──────────────────────────────────────────────────

class DIDWBAAuthClient:
    """Client for authenticating to agents via DID WBA."""
    
    def __init__(self, did_doc_path: str, private_key_path: str):
        self.authenticator = DIDWbaAuthHeader(
            did_document_path=did_doc_path,
            private_key_path=private_key_path,
        )
        self.token_cache: Dict[str, str] = {}
    
    async def authenticate(self, agent_card_url: str) -> bool:
        """
        Perform initial DID WBA authentication to get a Bearer token.
        Call this once per target agent before making A2A calls.
        """
        # Generate HTTP Signature headers for GET
        headers = self.authenticator.get_auth_header(
            agent_card_url,
            force_new=True,
            method="GET",
        )
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(agent_card_url, headers=headers)
                
                if resp.status_code == 200 and "authentication-info" in resp.headers:
                    self.authenticator.update_token(agent_card_url, dict(resp.headers))
                    auth_info = resp.headers["authentication-info"]
                    if 'access_token="' in auth_info:
                        token = auth_info.split('access_token="')[1].split('"')[0]
                        # Cache by base URL (without path) so it works for all endpoints on same host
                        base_url = self._get_base_url(agent_card_url)
                        self.token_cache[base_url] = token
                        trace.info("DID WBA token obtained for %s", base_url)
                        return True
        except Exception as e:
            trace.info("DID WBA auth exchange failed: %s", e)
        
        return False
    
    def get_auth_headers(self, target_url: str, method: str = "POST") -> Dict[str, str]:
        """
        Get auth headers for a request.
        If we have a cached Bearer token for this host, use it.
        Otherwise, fall back to HTTP Signature headers.
        """
        base_url = self._get_base_url(target_url)
        
        # Check for cached Bearer token
        if base_url in self.token_cache:
            trace.info("Using cached Bearer token for %s", base_url)
            return {"Authorization": f"Bearer {self.token_cache[base_url]}"}
        
        # No cached token - generate HTTP Signature for the specific method
        trace.info("No cached token for %s, using HTTP Signature", base_url)
        return self.authenticator.get_auth_header(
            target_url,
            force_new=True,
            method=method,
        )
    
    @staticmethod
    def _get_base_url(url: str) -> str:
        """Extract base URL (scheme + host + port) without path."""
        import re
        match = re.match(r'(https?://[^/]+)', url)
        return match.group(1) if match else url

# ── 1. PLAN ─────────────────────────────────────────────────────────────────

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

    # Guards (same as original)
    if plan["translate"] and not _needs_translation(text):
        trace.info("guard: dropping translate subtask (no translation keyword)")
        plan["translate"] = None

    referential = ("significa", "significan", "means", "meaning", "refiere", "refers")
    if (
        plan["translate"]
        and plan["answer"]
        and plan["translate"] not in plan["answer"]
        and any(marker in plan["answer"].lower() for marker in referential)
    ):
        trace.info("guard: attaching referenced text to question")
        plan["answer"] = f'{plan["answer"]} (the user request mentioned this text: "{plan["translate"]}")'

    if not plan["translate"] and not plan["answer"]:
        plan["answer"] = text
    trace.info("plan: translate=%r answer=%r", plan["translate"], plan["answer"])
    return plan


# ── 2. DISCOVER (ANP) ──────────────────────────────────────────────────────

async def _discover_agents_anp(skill: str) -> Optional[Dict[str, Any]]:
    """Discover agent via ANP Registry by skill."""
    discovery = ANPDiscoveryClient(ANP_REGISTRY_URL)
    return await discovery.find_agent(skill)


# ── 3. DELEGATE (A2A + DID WBA) ────────────────────────────────────────────

async def _call_agent_anp(
    agent_info: Dict[str, Any],
    text: str,
    did_auth: DIDWBAAuthClient,
    api_base: str,
    api_key: str,
    model: str,
) -> str:
    """
    A2A agent-to-agent call with DID WBA authentication.
    """
    
    # Build fixed hostname from DID
    did = agent_info.get("did", "")
    import re
    from urllib.parse import urlparse, urlunparse
    
    did_match = re.match(r'did:wba:([^:]+)', did)
    if not did_match:
        return "[invalid DID format]"
    
    did_hostname = did_match.group(1)
    parts = did_hostname.split('.')
    service_name = parts[0]
    if not service_name.endswith('-svc'):
        parts[0] = f"{service_name}-svc"
        fixed_hostname = '.'.join(parts)
    else:
        fixed_hostname = did_hostname
    
    # Step 1: Fetch agent card from sidecar proxy
    agent_card_url = f"http://{fixed_hostname}:8001/a2a-proxy/.well-known/agent-card.json"
    trace.info("Fetching agent card from: %s", agent_card_url)
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(agent_card_url)
        if resp.status_code != 200:
            return f"[failed to fetch agent card: {resp.status_code}]"
        
        agent_card = resp.json()
        a2a_url = agent_card.get("url")
        if not a2a_url:
            return "[no A2A URL in agent card]"
        
        trace.info("A2A endpoint from agent card: %s", a2a_url)
    
    # Step 2: Convert to sidecar proxy URL
    # Original: http://translator-with-sidecar-svc:8000/jsonrpc/
    # Target:   http://translator-with-sidecar-svc.a2a.svc.cluster.local:8001/a2a-proxy/jsonrpc/
    parsed = urlparse(a2a_url)
    
    # Use fixed hostname with port 8001
    new_netloc = f"{fixed_hostname}:8001"
    
    # Prepend /a2a-proxy to the path
    new_path = f"/a2a-proxy{parsed.path}"
    
    proxy_a2a_url = urlunparse((
        parsed.scheme,
        new_netloc,
        new_path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))
    
    trace.info("A2A proxy URL: %s", proxy_a2a_url)
    
    # Step 3: Authenticate via DID WBA
    auth_success = await did_auth.authenticate(agent_card_url)
    if auth_success:
        trace.info("DID WBA authentication completed")
    else:
        trace.info("DID WBA authentication failed, will try signature headers")
    
    # Step 4: Get auth headers for POST
    auth_headers = did_auth.get_auth_headers(proxy_a2a_url, method="POST")
    
    # Step 5: POST A2A task
    payload = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": str(uuid.uuid4()),
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": {
                    LLM_EXTENSION_URI: {
                        "llm_fulfillments": {
                            "default": {
                                "api_base": api_base,
                                "api_key": api_key,
                                "api_model": model,
                            }
                        }
                    }
                },
            }
        },
    }

    async with httpx.AsyncClient(event_hooks=_TRACE_HOOKS, timeout=60.0) as client:
        resp = await client.post(
            proxy_a2a_url,
            json=payload,
            headers={**auth_headers, "Content-Type": "application/json"},
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
        id="delegate-translation-anp",
        name="Delegate translation via ANP + A2A",
        description=(
            "Discovers translator agent via ANP Registry, authenticates with DID WBA, "
            "and delegates translation subtasks through A2A."
        ),
        tags=["orchestration", "anp", "a2a", "did-wba", "delegation", "translation"],
        examples=["Translate: Good morning, my friend!"],
        input_modes=["text"],
        output_modes=["text"],
    ),
    AgentSkill(
        id="delegate-answer-anp",
        name="Delegate answers via ANP + A2A",
        description="Discovers LLM agent via ANP Registry, authenticates with DID WBA, and delegates Q&A through A2A.",
        tags=["orchestration", "anp", "a2a", "did-wba", "delegation", "chat"],
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
async def orchestrator_v1_anp(
    input: Message,
    context: RunContext,
    llm: Annotated[LLMServiceExtensionServer, LLMServiceExtensionSpec.single_demand()],
    _platform_api: Annotated[PlatformApiExtensionServer, PlatformApiExtensionSpec()],
):
    """
    Planner/router agent with ANP + DID WBA integration.
    
    Decomposes user request with LLM, then for each subtask:
    1. Discovers agent via ANP Registry (by skill)
    2. Authenticates via DID WBA
    3. Delegates via A2A through ANP sidecar proxy
    """

    if not llm.data or not llm.data.llm_fulfillments:
        yield "No LLM model configured."
        return

    fulfillment = next(iter(llm.data.llm_fulfillments.values()))
    text = get_message_text(input)

    # Initialize DID WBA auth client
    try:
        did_auth = DIDWBAAuthClient(ORCHESTRATOR_DID_PATH, ORCHESTRATOR_KEY_PATH)
        trace.info("DID WBA auth initialized: %s", ORCHESTRATOR_DID_PATH)
    except Exception as e:
        trace.info("DID WBA init failed (%s); falling back to no auth", e)
        did_auth = None

    plan = await _plan(text, fulfillment.api_base, fulfillment.api_key, fulfillment.api_model)
    subtasks = [
        ("translation", plan["translate"], "🔀 ANP Discovery → DID Auth → A2A to **translator**...\n\n"),
        ("nlp", plan["answer"], "🔀 ANP Discovery → DID Auth → A2A to **llm_agent**...\n\n"),
    ]
    needed = [(skill, subtask, banner) for skill, subtask, banner in subtasks if subtask]

    first = True
    for skill, subtask, banner in needed:
        if not first:
            yield "\n\n"
        first = False

        yield banner
        
        # Discover agent via ANP
        agent = await _discover_agents_anp(skill)
        if not agent:
            yield f"[no agent found with skill '{skill}' in ANP Registry]"
            continue
        
        yield f"✅ Discovered: {agent['name']} ({agent['did'][:50]}...)\n"
        
        # Delegate via A2A + DID WBA
        if did_auth:
            yield await _call_agent_anp(
                agent,
                subtask,
                did_auth,
                fulfillment.api_base,
                fulfillment.api_key,
                fulfillment.api_model,
            )
        else:
            yield "[DID WBA not available]"


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