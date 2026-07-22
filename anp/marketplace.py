#!/usr/bin/env python3
"""
ANP Registry / Marketplace
--------------------------
Central discovery service for ANP agents in the K8s cluster.
- Accepts agent registrations
- Indexes by skills, name, DID
- Provides search and lookup APIs
"""

import json
import time
from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# =============================================================================
# DATA MODELS
# =============================================================================

class AgentRegistration(BaseModel):
    did: str = Field(..., description="Agent DID")
    ad_url: str = Field(..., description="URL to Agent Description JSON")
    name: str = Field(..., description="Agent name")
    description: str = Field(default="", description="Agent description")
    skills: List[str] = Field(default=[], description="Agent capabilities/skills")
    namespace: str = Field(default="default", description="K8s namespace")
    pod_name: str = Field(default="", description="K8s pod name")

class AgentRecord(BaseModel):
    did: str
    ad_url: str
    name: str
    description: str
    skills: List[str]
    namespace: str
    pod_name: str
    registered_at: float
    last_heartbeat: float
    status: str = "active"  # active, inactive, error

class SearchQuery(BaseModel):
    skills: Optional[List[str]] = None
    name: Optional[str] = None
    namespace: Optional[str] = None

# =============================================================================
# REGISTRY STORE (in-memory for now, can swap to SQLite/Postgres)
# =============================================================================

class RegistryStore:
    def __init__(self):
        self.agents: dict[str, AgentRecord] = {}  # did -> record
        self.by_skill: dict[str, set[str]] = {}   # skill -> set of DIDs
        self.by_name: dict[str, set[str]] = {}    # name -> set of DIDs
    
    def register(self, reg: AgentRegistration) -> AgentRecord:
        now = time.time()
        
        record = AgentRecord(
            did=reg.did,
            ad_url=reg.ad_url,
            name=reg.name,
            description=reg.description,
            skills=reg.skills,
            namespace=reg.namespace,
            pod_name=reg.pod_name,
            registered_at=now,
            last_heartbeat=now,
            status="active",
        )
        
        # Remove old entries for same DID
        if reg.did in self.agents:
            self._remove_from_indexes(reg.did)
        
        self.agents[reg.did] = record
        
        # Index by skills
        for skill in reg.skills:
            if skill not in self.by_skill:
                self.by_skill[skill] = set()
            self.by_skill[skill].add(reg.did)
        
        # Index by name
        if reg.name not in self.by_name:
            self.by_name[reg.name] = set()
        self.by_name[reg.name].add(reg.did)
        
        return record
    
    def _remove_from_indexes(self, did: str):
        if did not in self.agents:
            return
        old = self.agents[did]
        for skill in old.skills:
            self.by_skill.get(skill, set()).discard(did)
        self.by_name.get(old.name, set()).discard(did)
    
    def heartbeat(self, did: str) -> bool:
        if did not in self.agents:
            return False
        self.agents[did].last_heartbeat = time.time()
        self.agents[did].status = "active"
        return True
    
    def get(self, did: str) -> Optional[AgentRecord]:
        return self.agents.get(did)
    
    def search(self, skills: Optional[List[str]] = None, 
               name: Optional[str] = None,
               namespace: Optional[str] = None) -> List[AgentRecord]:
        results = list(self.agents.values())
        
        if skills:
            # Agents that have ANY of the specified skills
            dids = set()
            for skill in skills:
                dids.update(self.by_skill.get(skill, set()))
            results = [r for r in results if r.did in dids]
        
        if name:
            dids = self.by_name.get(name, set())
            results = [r for r in results if r.did in dids]
        
        if namespace:
            results = [r for r in results if r.namespace == namespace]
        
        return results
    
    def list_all(self) -> List[AgentRecord]:
        return list(self.agents.values())
    
    def cleanup_stale(self, max_age_seconds: int = 300):
        """Remove agents that haven't heartbeat in max_age_seconds."""
        now = time.time()
        stale = [did for did, rec in self.agents.items() 
                 if now - rec.last_heartbeat > max_age_seconds]
        for did in stale:
            self._remove_from_indexes(did)
            del self.agents[did]
            print(f"[Registry] Removed stale agent: {did}")

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(
    title="ANP Agent Registry",
    description="Discovery marketplace for ANP-enabled agents",
    version="1.0.0",
)

store = RegistryStore()

# =============================================================================
# ENDPOINTS
# =============================================================================

@app.post("/register", response_model=AgentRecord)
async def register_agent(reg: AgentRegistration):
    """Register a new agent or update existing one."""
    record = store.register(reg)
    print(f"[Registry] Registered: {reg.name} ({reg.did})")
    return record

@app.post("/heartbeat/{did}")
async def heartbeat(did: str):
    """Agent heartbeat to keep registration alive."""
    if not store.heartbeat(did):
        raise HTTPException(status_code=404, detail="Agent not registered")
    return {"status": "ok", "did": did}

@app.get("/agents")
async def list_agents(
    skill: Optional[str] = Query(None, description="Filter by skill"),
    name: Optional[str] = Query(None, description="Filter by name"),
    namespace: Optional[str] = Query(None, description="Filter by K8s namespace"),
):
    """List all registered agents, optionally filtered."""
    skills = [skill] if skill else None
    results = store.search(skills=skills, name=name, namespace=namespace)
    return {
        "count": len(results),
        "agents": [r.model_dump() for r in results],
    }

@app.get("/agents/{did}")
async def get_agent(did: str):
    """Get specific agent by DID."""
    agent = store.get(did)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent.model_dump()

@app.get("/search")
async def search_agents(
    skills: Optional[List[str]] = Query(None, description="Required skills"),
    namespace: Optional[str] = Query(None),
):
    """Search agents by capabilities."""
    results = store.search(skills=skills, namespace=namespace)
    return {
        "count": len(results),
        "agents": [r.model_dump() for r in results],
    }

@app.get("/skills")
async def list_skills():
    """List all known skills across registered agents."""
    return {
        "skills": sorted(store.by_skill.keys()),
        "counts": {skill: len(dids) for skill, dids in store.by_skill.items()},
    }

@app.delete("/agents/{did}")
async def unregister_agent(did: str):
    """Remove an agent from the registry."""
    if did not in store.agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    store._remove_from_indexes(did)
    del store.agents[did]
    return {"status": "removed", "did": did}

@app.get("/health")
async def health():
    """Registry health check."""
    return {
        "status": "healthy",
        "registered_agents": len(store.agents),
        "unique_skills": len(store.by_skill),
    }

# =============================================================================
# BACKGROUND TASK: Cleanup stale agents
# =============================================================================
@app.on_event("startup")
async def startup():
    import asyncio
    
    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            store.cleanup_stale(max_age_seconds=300)
    
    asyncio.create_task(cleanup_loop())

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)