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
    # PATCH existing provider with new location
    http_code=$(curl -s -o /dev/null -w "%{http_code}" -u "$AUTH" \
      -X PATCH "$PLATFORM/api/v1/providers/$existing_id" \
      -H "Content-Type: application/json" \
      -d "{\"location\": \"$location\"}")
    echo "updated ($existing_id) → $http_code"
  else
    # CREATE new provider
    result=$(curl -s -u "$AUTH" \
      -X POST "$PLATFORM/api/v1/providers" \
      -H "Content-Type: application/json" \
      -d "{\"location\": \"$location\"}")
    new_id=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id','?'))" 2>/dev/null)
    echo "created ($new_id)"
  fi
}

echo "Waiting for agents to be reachable..."
for svc in llm-agent-svc:8000 translator-svc:8000 orchestrator-svc:8000; do
  kubectl run --rm -n a2a check-"${svc%%:*}" \
    --image=curlimages/curl:latest --restart=Never -q \
    -- curl -sf "http://$svc/.well-known/agent-card.json" > /dev/null 2>&1 \
    && echo "  ✓ $svc reachable" || echo "  ✗ $svc NOT reachable (continue anyway)"
done

echo ""
echo "Registering agents:"
register "llm_agent"    "http://llm-agent-svc:8000#llm_agent"
register "translator"   "http://translator-svc:8000#translator"
register "orchestrator" "http://orchestrator-svc:8000#orchestrator"

echo ""
echo "Done. Current provider states:"
curl -s -u "$AUTH" "$PLATFORM/api/v1/providers" | \
  python3 -c "
import json, sys
for p in json.load(sys.stdin).get('items', []):
    card = p.get('agent_card') or {}
    name = card.get('name', '?')
    if name in ('llm_agent', 'translator', 'orchestrator', 'Chat'):
        print(f\"  {name:20} {p.get('state','?')}\")
"
