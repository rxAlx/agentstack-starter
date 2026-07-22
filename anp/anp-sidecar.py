#!/usr/bin/env python3
"""
ANP Sidecar for K8s Agent Pods
-------------------------------
- Serves DID document and Agent Description (AD)
- Authenticates incoming requests via DID WBA
- Forwards A2A tasks to the main agent container
- Registers with the ANP Registry on startup
"""

import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

# ANP imports
from anp.authentication import create_did_wba_document, DidWbaVerifier, DidWbaVerifierConfig
from anp.openanp.middleware import auth_middleware

# =============================================================================
# CONFIGURATION (from env vars or defaults)
# =============================================================================
AGENT_NAME = os.getenv("AGENT_NAME", "unknown-agent")
AGENT_DESCRIPTION = os.getenv("AGENT_DESCRIPTION", f"Agent {AGENT_NAME}")
AGENT_TYPE = os.getenv("AGENT_TYPE", "AgentService")  # e.g., Orchestrator, LLM, Translator
AGENT_SKILLS = os.getenv("AGENT_SKILLS", "").split(",") if os.getenv("AGENT_SKILLS") else []
AGENT_PORT = int(os.getenv("AGENT_PORT", "8001"))  # Sidecar port
MAIN_AGENT_PORT = int(os.getenv("MAIN_AGENT_PORT", "8080"))  # Main A2A agent port
MAIN_AGENT_HOST = os.getenv("MAIN_AGENT_HOST", "localhost")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://anp-registry:8080")
NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
POD_NAME = os.getenv("HOSTNAME", "unknown-pod")

# DID config
DID_DIR = Path("/app/did")
DID_DIR.mkdir(exist_ok=True)
DID_DOC_PATH = DID_DIR / "did.json"
PRIV_KEY_PATH = DID_DIR / "private-key.pem"

# JWT keys for verifying incoming requests (shared or per-agent)
JWT_PUB_PATH = DID_DIR / "jwt-public.pem"

# =============================================================================
# STEP 1: GENERATE OR LOAD DID
# =============================================================================
def setup_did():
    """Generate or load existing DID document and private key."""
    if DID_DOC_PATH.exists() and PRIV_KEY_PATH.exists():
        print(f"[{AGENT_NAME}] Loading existing DID...")
        with open(DID_DOC_PATH) as f:
            did_doc = json.load(f)
        return did_doc["id"]

    print(f"[{AGENT_NAME}] Generating new DID...")
    hostname = f"{AGENT_NAME}.{NAMESPACE}.svc.cluster.local"
    
    did_document, private_keys = create_did_wba_document(
        hostname=hostname,
        path_segments=["agent"],
    )

    # Save DID document
    with open(DID_DOC_PATH, "w") as f:
        json.dump(did_document, f, indent=2)

    # Save private key (dict of tuples: {key_id: (pem_bytes, pub_bytes)})
    first_key_id = list(private_keys.keys())[0]
    priv_key_pem = private_keys[first_key_id][0].decode() if isinstance(private_keys[first_key_id][0], bytes) else private_keys[first_key_id][0]
    
    with open(PRIV_KEY_PATH, "w") as f:
        f.write(priv_key_pem)

    did = did_document["id"]
    print(f"[{AGENT_NAME}] DID: {did}")
    return did

# =============================================================================
# STEP 2: CREATE FASTAPI APP
# =============================================================================
app = FastAPI(title=f"ANP Sidecar - {AGENT_NAME}")

# Load JWT public key for verifying incoming Bearer tokens
jwt_public_key = None
if JWT_PUB_PATH.exists():
    jwt_public_key = JWT_PUB_PATH.read_text()

# Configure verifier (if JWT pub key available)
verifier = None
if jwt_public_key:
    config = DidWbaVerifierConfig(
        jwt_public_key=jwt_public_key,
        jwt_algorithm="RS256",
        access_token_expire_minutes=60,
    )
    verifier = DidWbaVerifier(config)

# Exempt paths for DID document and health
exempt_paths = ["/health", "/agent/did.json", "/agent/ad.json", "/docs", "/openapi.json"]

if verifier:
    @app.middleware("http")
    async def did_wba_auth(request: Request, call_next):
        return await auth_middleware(request, call_next, verifier, exempt_paths=exempt_paths)

# =============================================================================
# ENDPOINTS
# =============================================================================

@app.on_event("startup")
async def startup():
    """Register with ANP Registry on startup."""
    global DID
    DID = setup_did()
    
    # Register with marketplace
    await register_with_registry()

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "agent": AGENT_NAME,
        "did": DID if 'DID' in globals() else None,
    }

@app.get("/agent/did.json")
async def serve_did():
    """Serve DID document for resolution."""
    if not DID_DOC_PATH.exists():
        raise HTTPException(status_code=404, detail="DID document not found")
    return json.loads(DID_DOC_PATH.read_text())

@app.get("/agent/ad.json")
async def serve_ad():
    """Serve ANP Agent Description."""
    hostname = f"{AGENT_NAME}.{NAMESPACE}.svc.cluster.local"
    
    ad = {
        "protocolType": "ANP",
        "protocolVersion": "1.0.0",
        "type": "Product",
        "url": f"http://{hostname}:{AGENT_PORT}/agent/ad.json",
        "identifier": DID if 'DID' in globals() else "unknown",
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "security": {
            "didwba": {
                "scheme": "didwba",
                "in": "header",
                "name": "Authorization"
            }
        },
        "brand": {
            "type": "Brand",
            "name": AGENT_NAME
        },
        "category": "Agent Service",
        "sku": DID if 'DID' in globals() else "unknown",
        "skills": AGENT_SKILLS,
        "interfaces": [
            {
                "type": "A2AInterface",
                "protocol": "a2a",
                "url": f"http://{hostname}:{MAIN_AGENT_PORT}/.well-known/agent.json",
                "description": f"{AGENT_NAME} A2A Agent Card"
            }
        ]
    }
    return ad

@app.post("/a2a-proxy/{path:path}")
async def proxy_to_a2a(path: str, request: Request):
    """
    Proxy A2A requests to the main agent container.
    This allows ANP-authenticated agents to call A2A endpoints.
    """
    body = await request.body()
    headers = dict(request.headers)
    
    # Remove hop-by-hop headers
    for h in ["host", "content-length", "connection"]:
        headers.pop(h, None)
    
    target_url = f"http://{MAIN_AGENT_HOST}:{MAIN_AGENT_PORT}/{path}"
    
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            timeout=30.0,
        )
        return JSONResponse(
            content=response.json() if response.headers.get("content-type", "").startswith("application/json") else {"response": response.text},
            status_code=response.status_code,
        )

# =============================================================================
# REGISTRY REGISTRATION
# =============================================================================
async def register_with_registry():
    """Register this agent with the ANP Registry."""
    if not REGISTRY_URL:
        print(f"[{AGENT_NAME}] No registry URL configured, skipping registration")
        return
    
    try:
        ad_url = f"http://{AGENT_NAME}.{NAMESPACE}.svc.cluster.local:{AGENT_PORT}/agent/ad.json"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{REGISTRY_URL}/register",
                json={
                    "did": DID if 'DID' in globals() else "unknown",
                    "ad_url": ad_url,
                    "name": AGENT_NAME,
                    "description": AGENT_DESCRIPTION,
                    "skills": AGENT_SKILLS,
                    "namespace": NAMESPACE,
                    "pod_name": POD_NAME,
                },
                timeout=10.0,
            )
            
            if response.status_code == 200:
                print(f"[{AGENT_NAME}] Registered with ANP Registry: {response.json()}")
            else:
                print(f"[{AGENT_NAME}] Registry registration failed: {response.status_code} - {response.text}")
                
    except Exception as e:
        print(f"[{AGENT_NAME}] Failed to register with registry: {e}")

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT)