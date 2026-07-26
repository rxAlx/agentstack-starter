#!/usr/bin/env python3
"""
Local DID WBA Authentication Test
---------------------------------
Tests DID WBA auth using localhost port-forwarded sidecars.
"""

import json
import asyncio
import httpx
from pathlib import Path
import tempfile

# ANP imports
from anp.authentication import create_did_wba_document, DIDWbaAuthHeader


# =============================================================================
# PATCH: Local DID resolver for testing
# =============================================================================
import anp.authentication.did_wba_verifier as verifier_module

_original_resolver = verifier_module.resolve_did_wba_document

async def local_http_resolver(did: str) -> dict:
    """Resolve DID documents from localhost instead of cluster DNS."""
    # Map DIDs to localhost ports
    did_to_localhost = {
        "orchestrator.a2a.svc.cluster.local": "http://localhost:8001/agent/did.json",
        "llm-agent-with-sidecar.a2a.svc.cluster.local": "http://localhost:8002/agent/did.json",
        "translator-with-sidecar.a2a.svc.cluster.local": "http://localhost:8003/agent/did.json",
    }
    
    # Extract hostname from DID
    # did:wba:HOSTNAME:agent:e1_...
    parts = did.split(":")
    if len(parts) >= 3:
        hostname = parts[2]
        for key, url in did_to_localhost.items():
            if key in hostname:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url)
                    return resp.json()
    
    # Fallback to original resolver
    return await _original_resolver(did)

verifier_module.resolve_did_wba_document = local_http_resolver
print("✅ Patched DID resolver for localhost testing")


async def test_did_resolution():
    """Test 1: Resolve DID documents via localhost."""
    print("\n" + "=" * 60)
    print("TEST 1: DID Document Resolution (via localhost)")
    print("=" * 60)
    
    async with httpx.AsyncClient() as client:
        for port, name in [(8001, "orchestrator"), (8002, "llm-agent"), (8003, "translator")]:
            resp = await client.get(f"http://localhost:{port}/agent/did.json")
            did_doc = resp.json()
            print(f"\n✅ {name}")
            print(f"   DID: {did_doc['id']}")
            print(f"   Keys: {len(did_doc['verificationMethod'])}")


async def test_agent_description():
    """Test 2: Fetch Agent Descriptions."""
    print("\n" + "=" * 60)
    print("TEST 2: Agent Description (AD)")
    print("=" * 60)
    
    async with httpx.AsyncClient() as client:
        for port, name in [(8001, "orchestrator"), (8002, "llm-agent"), (8003, "translator")]:
            resp = await client.get(f"http://localhost:{port}/agent/ad.json")
            ad = resp.json()
            print(f"\n✅ {ad['name']}")
            print(f"   Skills: {', '.join(ad['skills'])}")
            print(f"   A2A URL: {ad['interfaces'][0]['url']}")


async def test_authentication():
    """Test 3: Full DID WBA authentication flow."""
    print("\n" + "=" * 60)
    print("TEST 3: DID WBA Authentication")
    print("=" * 60)
    
    # Generate a test client DID
    print("\n1. Generating test client DID...")
    did_document, private_keys = create_did_wba_document(
        hostname="test-client.localhost",
        path_segments=["test"],
    )
    
    # Save to temp files
    tmpdir = Path(tempfile.mkdtemp())
    
    did_path = tmpdir / "did.json"
    with open(did_path, "w") as f:
        json.dump(did_document, f)
    
    key_path = tmpdir / "private.pem"
    first_key_id = list(private_keys.keys())[0]
    priv_key = private_keys[first_key_id][0]
    if isinstance(priv_key, bytes):
        priv_key = priv_key.decode()
    with open(key_path, "w") as f:
        f.write(priv_key)
    
    print(f"   Client DID: {did_document['id']}")
    
    # Create authenticator
    print("\n2. Creating authenticator...")
    authenticator = DIDWbaAuthHeader(
        did_document_path=str(did_path),
        private_key_path=str(key_path),
    )
    
    # Target: orchestrator health endpoint (exempt, but let's test auth headers)
    # For a real auth test, we need a protected endpoint
    # Let's test against the orchestrator's proxy (which may require auth)
    target_url = "http://localhost:8001/a2a-proxy/.well-known/agent-card.json"
    
    print(f"\n3. Sending authenticated request to: {target_url}")
    
    async with httpx.AsyncClient() as client:
        # First request: HTTP Message Signatures
        headers = authenticator.get_auth_header(
            target_url,
            force_new=True,
            method="GET",
        )
        print(f"   Signature-Input: {headers.get('Signature-Input', 'N/A')[:80]}...")
        
        resp = await client.get(target_url, headers=headers)
        print(f"\n4. Response: {resp.status_code}")
        
        if resp.status_code == 200:
            print(f"   ✅ Request authenticated successfully!")
            print(f"   Response headers: {dict(resp.headers)}")
            
            # Check for Bearer Token
            if "authentication-info" in resp.headers:
                print(f"\n5. ✅ Bearer Token received!")
                authenticator.update_token(target_url, dict(resp.headers))
                
                # Second request with Bearer Token
                print("\n6. Sending request with Bearer Token...")
                headers2 = authenticator.get_auth_header(target_url)
                resp2 = await client.get(target_url, headers=headers2)
                print(f"   Status: {resp2.status_code}")
            else:
                print(f"\n5. ℹ️ No Bearer Token (endpoint may be exempt or auth not enforced)")
        else:
            print(f"   ❌ Failed: {resp.text[:200]}")


async def test_registry():
    """Test 4: Query registry."""
    print("\n" + "=" * 60)
    print("TEST 4: Registry Query")
    print("=" * 60)
    
    async with httpx.AsyncClient() as client:
        resp = await client.get("http://localhost:8081/agents")
        results = resp.json()
        print(f"\n✅ {results['count']} agents registered:")
        for agent in results["agents"]:
            print(f"   - {agent['name']}: {', '.join(agent['skills'])}")


async def main():
    print("\n" + "=" * 60)
    print("Local DID WBA Authentication Test")
    print("=" * 60)
    print("\nPrerequisites:")
    print("  - kubectl port-forward deployment/orchestrator-with-sidecar 8001:8001 -n a2a")
    print("  - kubectl port-forward deployment/llm-agent-with-sidecar 8002:8001 -n a2a")
    print("  - kubectl port-forward deployment/translator-with-sidecar 8003:8001 -n a2a")
    print("  - kubectl port-forward svc/anp-registry 8081:8080 -n a2a")
    
    try:
        await test_did_resolution()
        await test_agent_description()
        await test_authentication()
        await test_registry()
        
        print("\n" + "=" * 60)
        print("ALL LOCAL TESTS COMPLETED")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())