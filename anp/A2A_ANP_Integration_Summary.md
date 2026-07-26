# A2A + ANP Integration Project

## Overview

This project integrates **Google's A2A (Agent-to-Agent) Protocol** with **ANP (Agent Network Protocol)** to create a hybrid multi-agent system running on Kubernetes. A2A handles task delegation and execution, while ANP provides decentralized identity (DIDs) and agent discovery.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster (a2a namespace)                   │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    ANP Registry (Marketplace)                        │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │    │
│  │  │  FastAPI    │  │  In-Memory  │  │  Health     │                 │    │
│  │  │  Registry   │  │  Store      │  │  Checks     │                 │    │
│  │  │  (port 8080)│  │             │  │             │                 │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                              ▲                                               │
│                              │ register / heartbeat                         │
│         ┌────────────────────┼────────────────────┐                         │
│         │                    │                    │                         │
│         ▼                    ▼                    ▼                         │
│  ┌─────────────┐      ┌─────────────┐      ┌─────────────┐                │
│  │ orchestrator│      │  llm-agent  │      │  translator │                │
│  │  + ANP      │      │   + ANP     │      │   + ANP     │                │
│  │  Sidecar    │      │  Sidecar    │      │  Sidecar    │                │
│  │             │      │             │      │             │                │
│  │ ┌─────────┐ │      │ ┌─────────┐ │      │ ┌─────────┐ │                │
│  │ │ A2A     │ │◄────►│ │ A2A     │ │◄────►│ │ A2A     │ │  (A2A mesh)   │
│  │ │ Agent   │ │      │ │ Agent   │ │      │ │ Agent   │ │                │
│  │ │ (8080)  │ │      │ │ (8080)  │ │      │ │ (8080)  │ │                │
│  │ └─────────┘ │      │ └─────────┘ │      │ └─────────┘ │                │
│  │ ┌─────────┐ │      │ ┌─────────┐ │      │ ┌─────────┐ │                │
│  │ │ ANP     │ │      │ │ ANP     │ │      │ │ ANP     │ │                │
│  │ │ Sidecar │ │      │ │ Sidecar │ │      │ │ Sidecar │ │                │
│  │ │ (8001)  │ │      │ │ (8001)  │ │      │ │ (8001)  │ │                │
│  │ │ - DID   │ │      │ │ - DID   │ │      │ │ - DID   │ │                │
│  │ │ - AD    │ │      │ │ - AD    │ │      │ │ - AD    │ │                │
│  │ │ - Auth  │ │      │ │ - Auth  │ │      │ │ - Auth  │ │                │
│  │ └─────────┘ │      │ └─────────┘ │      │ └─────────┘ │                │
│  └─────────────┘      └─────────────┘      └─────────────┘                │
│                                                                              │
│  Sidecar Endpoints:                                                         │
│  - /health              → Health check + DID                               │
│  - /agent/ad.json       → ANP Agent Description (discovery)                │
│  - /agent/did.json      → DID Document (W3C standard)                      │
│  - /a2a-proxy/{path}    → Proxy to A2A endpoints                           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## What We've Built

### 1. ANP Sidecar Container (`anp-sidecar`)

A Python/FastAPI sidecar that runs alongside each A2A agent in the same pod.

**Features:**

- **DID Generation**: Auto-generates `did:wba` identities using ANP SDK
- **Agent Description**: Serves `ad.json` for ANP discovery
- **DID Document**: Serves `did.json` for authentication resolution
- **DID WBA Auth**: HTTP Message Signatures + Bearer Token authentication
- **A2A Proxy**: Forwards requests to the main A2A agent container
- **Auto-Registration**: Registers with ANP Registry on startup

**Key Files:**

- `anp/anp-sidecar.py` — Sidecar application code
- `docker/Dockerfile.anp-sidecar` — Container build
- `.github/workflows/publish-sidecar.yaml` — CI/CD pipeline

**Environment Variables:**

| Variable              | Description                  | Default                      |
| --------------------- | ---------------------------- | ---------------------------- |
| `AGENT_NAME`        | Agent identifier             | `unknown-agent`            |
| `AGENT_DESCRIPTION` | Human-readable description   | `"Agent {name}"`           |
| `AGENT_SKILLS`      | Comma-separated capabilities | `""`                       |
| `AGENT_PORT`        | Sidecar HTTP port            | `8000`                     |
| `MAIN_AGENT_PORT`   | A2A agent port               | `8080`                     |
| `MAIN_AGENT_HOST`   | A2A agent hostname           | `localhost`                |
| `REGISTRY_URL`      | ANP Registry URL             | `http://anp-registry:8080` |

---

### 2. ANP Registry / Marketplace (`anp-registry`)

A FastAPI-based discovery service that indexes all registered agents.

**Features:**

- **Agent Registration**: `POST /register` — Accepts agent metadata
- **Agent Listing**: `GET /agents` — List all registered agents
- **Skill Search**: `GET /search?skills=...` — Find agents by capability
- **DID Lookup**: `GET /agents/{did}` — Get specific agent details
- **Skill Catalog**: `GET /skills` — List all known skills
- **Health Checks**: Automatic stale agent cleanup

**Key Files:**

- `anp/marketplace.py` — Registry application code
- `docker/Dockerfile.anp-registry` — Container build
- `.github/workflows/publish-marketplace.yaml` — CI/CD pipeline

---

### 3. K8s Deployments

**Orchestrator with Sidecar (Completed):**

- File: `deploy/k8s/agents/orchestrator-with-sidecar.yaml`
- Status: ✅ Running and registered
- DID: `did:wba:orchestrator.a2a.svc.cluster.local:agent:e1_...`

**ANP Registry (Completed):**

- File: `deploy/k8s/anp-registry.yaml`
- Status: ✅ Running
- Service: `anp-registry.a2a.svc.cluster.local:8080`

---

### 4. CI/CD Pipelines

| Workflow                     | Trigger                  | Purpose                              |
| ---------------------------- | ------------------------ | ------------------------------------ |
| `publish.yaml`             | `*_v*.*` tags (agents) | Build A2A agents via Agent Stack CLI |
| `publish-sidecar.yaml`     | `anp-sidecar_v*.*`     | Build ANP sidecar (plain Docker)     |
| `publish-marketplace.yaml` | `anp-registry_v*.*`    | Build ANP registry (plain Docker)    |

---

## Verified Functionality

| Test                    | Status | Command                                                  |
| ----------------------- | ------ | -------------------------------------------------------- |
| Sidecar health endpoint | ✅     | `curl localhost:8001/health`                           |
| Agent Description (AD)  | ✅     | `curl localhost:8001/agent/ad.json`                    |
| DID Document            | ✅     | `curl localhost:8001/agent/did.json`                   |
| Registry registration   | ✅     | Auto on startup                                          |
| Registry agent listing  | ✅     | `curl localhost:8081/agents`                           |
| A2A proxy (GET)         | ✅     | `curl localhost:8001/a2a-proxy/.well-known/agent.json` |

---

## TODO List

### High Priority

- [ ] **Deploy ANP sidecars for remaining agents**

  - [ ] `llm-agent-with-sidecar.yaml`
  - [ ] `translator-with-sidecar.yaml`
- [ ] **Test cross-agent discovery via registry**

  - [ ] Query by skill: `curl /search?skills=translation`
  - [ ] Verify all 3 agents appear in `/agents`
- [ ] **Test DID WBA authentication flow**

  - [ ] Client generates HTTP Message Signatures
  - [ ] Server verifies DID + issues Bearer Token
  - [ ] Subsequent requests use Bearer Token

### Medium Priority

- [ ] **Implement BlockA2A security layer** (from paper)

  - [ ] Blockchain-anchored audit log for agent interactions
  - [ ] Smart contract access control policies
  - [ ] Defense Orchestration Engine (DOE) for attack detection
- [ ] **Add PostgreSQL persistence to registry**

  - [ ] Replace in-memory store with database
  - [ ] Add agent heartbeat tracking
- [ ] **Create Mermaid architecture diagram**

  - [ ] BlockA2A + ANP + A2A integration
  - [ ] For documentation/presentation

### Low Priority / Future Work

- [ ] **ANP marketplace web UI**

  - [ ] Browse agents by skill/category
  - [ ] Visual agent topology map
- [ ] **Integration with external ANP marketplace**

  - [ ] Register agents with public ANP indexers
  - [ ] Cross-cluster agent discovery
- [ ] **Performance optimization**

  - [ ] Sidecar resource limits
  - [ ] Registry caching layer
- [ ] **Monitoring & observability**

  - [ ] Prometheus metrics for sidecars
  - [ ] Grafana dashboard for registry
  - [ ] A2A interaction tracing

---

## Quick Commands Reference

```bash
# Port-forward for local testing
kubectl port-forward deployment/orchestrator-with-sidecar 8001:8001 -n a2a
kubectl port-forward svc/anp-registry 8081:8080 -n a2a

# Test sidecar endpoints
curl http://localhost:8001/health | jq .
curl http://localhost:8001/agent/ad.json | jq .
curl http://localhost:8001/agent/did.json | jq .

# Test A2A proxy
curl http://localhost:8001/a2a-proxy/.well-known/agent.json | jq .

# Test registry
curl http://localhost:8081/health
curl http://localhost:8081/agents | jq .
curl "http://localhost:8081/search?skills=workflow" | jq .

# Build and push sidecar
docker build --no-cache -f docker/Dockerfile.anp-sidecar -t ghcr.io/rxalx/agentstack-starter/anp-sidecar:v1.0.3 .
docker push ghcr.io/rxalx/agentstack-starter/anp-sidecar:v1.0.3

# Deploy
git tag anp-sidecar_v1.0.3 && git push origin anp-sidecar_v1.0.3
kubectl apply -f deploy/k8s/agents/orchestrator-with-sidecar.yaml
kubectl apply -f deploy/k8s/anp-registry.yaml
```

---

## Key Learnings

1. **ANP SDK `create_did_wba_document()` returns a dict** of private keys, not a list — keys are indexed by `key_id` (e.g., `"key-1"`)
2. **Dockerfile CMD matters**: `CMD ["uvicorn", "sidecar:app"]` ignores env vars; use `CMD ["python", "sidecar.py"]` to read `AGENT_PORT` at runtime
3. **DID resolution defaults to HTTPS:443** — patch needed for local HTTP testing
4. **Port conflicts in same pod**: Containers share network namespace; use different ports (8000 vs 8001)
5. **K8s DNS**: Services in same namespace resolve as `<service-name>`; cross-namespace needs `<service>.<namespace>.svc.cluster.local`

---

*Last updated: 2026-07-22*
