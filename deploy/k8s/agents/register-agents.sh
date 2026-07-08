#!/usr/bin/env bash
# Registers (or updates) the three custom agents in AgentStack.
# Run this from the HOST after deploying k8s-agents/ manifests.
#
# Usage:  bash k8s-agents/register-agents.sh

set -euo pipefail

PLATFORM="http://localhost:8333"
AUTH="admin:admin123"

register() {
  local name="$1" location="$2"
  echo -n "→ $name ... "

  # Check if already registered
  existing_id=$(curl -s -u "$AUTH" "$PLATFORM/api/v1/providers" | \
    python3 -c "
import json, sys
for p in json.load(sys.stdin).get('items', []):
    card = p.get('agent_card') or {}
    if card.get('name') == '$name':
        print(p['id']); break
" 2>/dev/null)

  if [ -n "$existing_id" ]; then
    # DELETE old registration (PATCH does not support updating location)
    curl -s -o /dev/null -u "$AUTH" -X DELETE "$PLATFORM/api/v1/providers/$existing_id"
    echo -n "deleted ($existing_id), "
  fi

  # CREATE with k8s service URL
  result=$(curl -s -u "$AUTH" \
    -X POST "$PLATFORM/api/v1/providers" \
    -H "Content-Type: application/json" \
    -d "{\"location\": \"$location\"}")
  new_id=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id','?'))" 2>/dev/null)
  echo "created ($new_id)"
}

echo "Waiting for agents to be reachable..."
for svc in llm-agent-svc:8000 translator-svc:8000 orchestrator-svc:8000 orchestrator-v1-svc:8000; do
  kubectl run --rm -n a2a check-"${svc%%:*}" \
    --image=curlimages/curl:latest --restart=Never -q \
    -- curl -sf "http://$svc/.well-known/agent-card.json" > /dev/null 2>&1 \
    && echo "  ✓ $svc reachable" || echo "  ✗ $svc NOT reachable (continue anyway)"
done

echo ""
echo "Registering agents:"
register "llm_agent"       "http://llm-agent-svc:8000#llm_agent"
register "translator"      "http://translator-svc:8000#translator"
register "orchestrator"    "http://orchestrator-svc:8000#orchestrator"
register "orchestrator_v1" "http://orchestrator-v1-svc:8000#orchestrator_v1"

echo ""
echo "Done. Current provider states:"
curl -s -u "$AUTH" "$PLATFORM/api/v1/providers" | \
  python3 -c "
import json, sys
for p in json.load(sys.stdin).get('items', []):
    card = p.get('agent_card') or {}
    name = card.get('name', '?')
    if name in ('llm_agent', 'translator', 'orchestrator', 'orchestrator_v1', 'Chat'):
        print(f\"  {name:20} {p.get('state','?')}\")
"

echo ""
echo "Removing duplicate registrations (keeps newest per agent)..."
STALE_IDS=$(curl -s -u "$AUTH" "$PLATFORM/api/v1/providers" | \
  python3 -c "
import json, sys
from collections import defaultdict
groups = defaultdict(list)
for p in json.load(sys.stdin).get('items', []):
    name = (p.get('agent_card') or {}).get('name', '?')
    if name in ('llm_agent', 'translator', 'orchestrator', 'orchestrator_v1'):
        groups[name].append(p)
for providers in groups.values():
    for p in sorted(providers, key=lambda x: x['created_at'], reverse=True)[1:]:
        print(p['id'])
")
if [ -z "$STALE_IDS" ]; then
  echo "  none found"
else
  echo "$STALE_IDS" | while read stale_id; do
    curl -s -o /dev/null -u "$AUTH" -X DELETE "$PLATFORM/api/v1/providers/$stale_id"
    echo "  deleted $stale_id"
  done
fi
