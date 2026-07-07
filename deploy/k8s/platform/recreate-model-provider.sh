#!/usr/bin/env bash
# Recreates the Groq model provider in AgentStack.
#
# WHY: AgentStack 0.7.1 only populates the OpenAI-gateway model catalog when a
# model provider is CREATED — it is not rebuilt when agentstack-server restarts.
# After any server restart (helm upgrade, rollout restart...) the catalog is
# empty and every LLM call fails with:
#   APIError: model_provider with id openai:<model> not found
# Recreating the provider repopulates the catalog immediately.
#
# Usage:  bash deploy/k8s/platform/recreate-model-provider.sh [path-to-groq-key]
# Requires: kubectl port-forward -n a2a svc/agentstack-server-svc 8333:8333

set -euo pipefail

PLATFORM="http://localhost:8333"
AUTH="admin:admin123"
KEY_FILE="${1:-$(dirname "$0")/../../../../grok_api_key.txt}"

GROQ_KEY=$(tr -d '[:space:]' < "$KEY_FILE")
[ -n "$GROQ_KEY" ] || { echo "ERROR: empty API key in $KEY_FILE"; exit 1; }

echo -n "→ Groq provider: "
GROQ_ID=$(curl -s -u "$AUTH" "$PLATFORM/api/v1/model_providers" | python3 -c "
import json, sys
for m in json.load(sys.stdin).get('items', []):
    if m['name'] == 'Groq':
        print(m['id']); break
")

if [ -n "$GROQ_ID" ]; then
  curl -s -o /dev/null -u "$AUTH" -X DELETE "$PLATFORM/api/v1/model_providers/$GROQ_ID"
  echo -n "deleted ($GROQ_ID), "
fi

NEW_ID=$(curl -s -u "$AUTH" -X POST "$PLATFORM/api/v1/model_providers" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Groq\",\"type\":\"openai\",\"base_url\":\"https://api.groq.com/openai/v1/\",\"api_key\":\"$GROQ_KEY\"}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id','?'))")
echo "created ($NEW_ID)"

MODELS=$(curl -s -u "$AUTH" "$PLATFORM/api/v1/openai/models" \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('data',[])))")
echo "→ model catalog now has $MODELS models"
[ "$MODELS" -gt 0 ] && echo "✓ OK" || { echo "✗ catalog still empty"; exit 1; }
