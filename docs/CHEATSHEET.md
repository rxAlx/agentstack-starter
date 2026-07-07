 

# Cheat sheet de comandos para interactuar con Agent Stack

Variables globales:

- Port forward del agent server en 8333
- Credenciales del keycloak para acceder via CLI al agent server

```Shell
PLATFORM=http://localhost:8333
AUTH="admin:admin123"   # AgentStack 0.7.1 usa credenciales de Keycloak → usar siempre: curl -u $AUTH
```

---

```Shell
kubectl port-forward svc/agentstack-server-svc 8333:8333 -n a2a &
kubectl port-forward svc/agentstack-ui-svc     8334:8334 -n a2a &
kubectl port-forward svc/keycloak              8336:8336 -n a2a &
```

---

## 1. Ver providers registrados (y sus IDs)

```bash
curl -s -u $AUTH $PLATFORM/api/v1/providers | \
  python3 -c "
import sys, json
for p in json.load(sys.stdin)['items']:
    print(p['state'], p['id'], p['agent_card']['name'])
"
```

## 2. Ver la Agent Card de un agente

```bash
PROVIDER_ID=<id>

curl -s -u $AUTH \
  $PLATFORM/api/v1/a2a/$PROVIDER_ID/.well-known/agent-card.json | jq .
```

Devuelve: nombre, descripción, capacidades, extensiones requeridas, transportes.

---

## 3. Llamar al proxy OpenAI del platform (LLM directo)

```bash
curl -s -u $AUTH \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai:llama-3.1-8b-instant",
    "messages": [{"role": "user", "content": "Hola, quién eres?"}],
    "stream": false
  }' \
  $PLATFORM/api/v1/openai/chat/completions | jq .choices[0].message.content
```

---

## 4. Enviar una query a un agente via A2A (JSON-RPC)

```bash
PLATFORM=http://localhost:8333
AUTH="admin:admin123"
PROVIDER_ID=49a37b0e-1797-37bd-00ca-214f3a906d86 #translator

# 1) Contexto + context token (una vez por sesión)
CTX_ID=$(curl -s -u $AUTH -X POST $PLATFORM/api/v1/contexts \
  -H "Content-Type: application/json" -d '{}' | jq -r .id)

TOKEN=$(curl -s -u $AUTH -X POST $PLATFORM/api/v1/contexts/$CTX_ID/token \
  -H "Content-Type: application/json" -d '{
    "grant_global_permissions": {"llm": ["*"], "a2a_proxy": ["*"]},
    "grant_context_permissions": {"files": ["*"], "vector_stores": ["*"], "context_data": ["*"]}
  }' | jq -r .token)

# 2) message/send con Bearer y el token como api_key
curl -s -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "method": "message/send", "id": "1",
    "params": { "message": {
      "messageId": "msg-001", "role": "user",
      "parts": [{"kind": "text", "text": "¿Cuál es la capital de Japón?"}],
      "metadata": {
        "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1": {
          "llm_fulfillments": { "default": {
            "api_base": "{platform_url}/api/v1/openai",
            "api_key": "'$TOKEN'",
            "api_model": "openai:llama-3.1-8b-instant"
          }}}}}}}' | jq .
```

Extraer solo el texto de la respuesta del agente:

```bash
... | jq '[.result.history[] | select(.role=="agent") | .parts[].text] | join("")'
```

---

## 5. Conexión a GROQ

Para ver los modelos disponibles (LLM) de GROQ desde dentro del pod Agent Stack (podría acceder desde internet en lugar desde dentro de un POD)

Esto me permite saber que modelos LLM de GROQ puedo usar para crear mis agentes.

Para saber más sobre los tokens restantes y mi api key de GROQ acceder a: [console.groq.com/settings/organization/usage?tab=activity&amp;dateRange=%7B%22from%22%3A%222026-06-02%22%2C%22to%22%3A%222026-07-01%22%7D](https://console.groq.com/settings/organization/usage?tab=activity&dateRange=%7B%22from%22%3A%222026-06-02%22%2C%22to%22%3A%222026-07-01%22%7D)

```Shell
GROQ_KEY=$(cat /home/chaumepgtec/Documentos/VRAIN/a2a/mv_agentstack/grok_api_key.txt | tr -d '[:space:]')
SERVER_POD=$(kubectl get pods -n a2a -o name | grep agentstack-server | head -1 | cut -d/ -f2)
kubectl exec -n a2a "$SERVER_POD" -- python3 -c "
import urllib.request
req=urllib.request.Request('https://api.groq.com/openai/v1/models',
    headers={'Authorization':'Bearer $GROQ_KEY','User-Agent':'curl/8.5.0'})
try:
    r=urllib.request.urlopen(req,timeout=10)
    body=r.read()[:120]
    print('status', r.status, body[:80])
except Exception as e:
    print('ERROR:', e)
" 2>&1 | tail -2
```

---

---

## 6. Comandos para bugs

### 6.1 Añadir model providers al catálogo de Agent Stack

Se debe ejecutar este comando cada vez que se modifique el values del Helm de Agent Stack y se reinicie el pod de Agent Stack. Si no se ejecuta en la UI del Agent Stack al preguntar a cualquier agente da error de API: Model provider not found. (se trata de un bug de Agent Stack 0.7.1)

```Shell
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
```

### 6.2 Keycloak: restaurar token lifetime tras helm upgrade

`next-auth@5.0.0-beta.30` no soporta HTTP token refresh → tokens de 8h como workaround.
El job `keycloak-provision` resetea el realm en cada upgrade, así que re-ejecutar:

```bash
KC_TOKEN=$(curl -s -X POST http://localhost:8336/realms/master/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=keycloak-admin-password&grant_type=password&client_id=admin-cli" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -s -o /dev/null -w "%{http_code}" -X PUT http://localhost:8336/admin/realms/agentstack \
  -H "Authorization: Bearer $KC_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"accessTokenLifespan":28800,"ssoSessionIdleTimeout":28800,"ssoSessionMaxLifespan":86400,"clientSessionIdleTimeout":28800,"clientSessionMaxLifespan":86400}'
# Debe devolver 204
```

---

## 7. Protocolo A2A avanzado

> Requiere `$TOKEN` (context token) y `$PROVIDER_ID` de la sección 4.
> `message/send` y `message/stream` son los dos modos de entrega del protocolo;
> `tasks/get` y `tasks/cancel` son el ciclo de vida de las Tasks.

### 7.1 message/stream: respuesta en streaming (SSE)

Mismo envelope JSON-RPC que `message/send`de la sección 4, pero la respuesta es un stream de
eventos Server-Sent Events: `status-update` con `state=submitted → working (un evento por chunk del LLM) → completed` y `final:true` en el último.

```bash
curl -sN -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "method": "message/stream", "id": "1",
    "params": { "message": {
      "messageId": "msg-002", "role": "user",
      "parts": [{"kind": "text", "text": "Hola amigo"}],
      "metadata": {
        "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1": {
          "llm_fulfillments": { "default": {
            "api_base": "{platform_url}/api/v1/openai",
            "api_key": "'$TOKEN'",
            "api_model": "openai:llama-3.1-8b-instant"
          }}}}}}}'
```

Ver solo los chunks de texto:

```bash
curl -sN -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "method": "message/stream", "id": "1",
    "params": { "message": {
      "messageId": "msg-002", "role": "user",
      "parts": [{"kind": "text", "text": "Hola amigo"}],
      "metadata": {
        "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1": {
          "llm_fulfillments": { "default": {
            "api_base": "{platform_url}/api/v1/openai",
            "api_key": "'$TOKEN'",
            "api_model": "openai:llama-3.1-8b-instant"
          }}}}}}}' | grep "^data:" | sed 's/^data: //' | \
  jq -r 'select(.result.kind=="status-update") | .result.status.message.parts[]?.text // empty'
```

### 7.2 tasks/get — recuperar una Task por id (auditoría)

La Task persiste tras completarse; cualquier cliente autorizado puede consultarla.
El `taskId` viene en `.result.id` de la respuesta de `message/send`.

Es clave para el concepto de espacio de agentes (trazabilidad de interacciones)

```bash
TASK_ID=<id>

curl -s -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/get","id":"1",
       "params":{"id":"'$TASK_ID'","historyLength":10}}' | \
  jq '{state: .result.status.state, history: (.result.history | length)}'
```

### 7.3 tasks/cancel — cancelar una Task

Sobre una task ya completada devuelve el error estándar del protocolo
`-32002 TaskNotCancelable` — útil para ver los códigos de error A2A en acción.

Ver solo el status de la task:

```bash
curl -s -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/cancel","id":"1","params":{"id":"'$TASK_ID'"}}' | jq .
```

Ver toda la task:

```Shell
curl -s -X POST "$PLATFORM/api/v1/a2a/$PROVIDER_ID/"   -H "Authorization: Bearer $TOKEN"   -H "Content-Type: application/json"   -d '{"jsonrpc":"2.0","method":"tasks/get","id":"1",
       "params":{"id":"'$TASK_ID'","historyLength":10}}' |   jq .
```

### 7.4 Multi-turno

Para continuar una conversación, debo reutilizar en el siguiente mensaje los campos que devolvió la Task anterior: `"contextId"` (hilo de conversación) y opcionalmente `"taskId"` (continuar esa task concreta). Ambos van como campos del objeto `message`, al lado de `messageId`. Ejemplo:

```
```

---

## 8. Keycloak (identidad detrás de Agent Stack)

> Requiere el port-forward 8336 y `$KC_TOKEN` de la sección 6.2.

### 8.1 Usuarios y clients del realm `agentstack`

```bash
# Usuarios (admin y agent-user vienen seeded por el helm chart)
curl -s -H "Authorization: Bearer $KC_TOKEN" \
  http://localhost:8336/admin/realms/agentstack/users | \
  jq -r '.[] | "\(.username)  \(.email)  enabled=\(.enabled)"'

# Clients OIDC (agentstack-server, agentstack-ui, agentstack-cli...)
curl -s -H "Authorization: Bearer $KC_TOKEN" \
  http://localhost:8336/admin/realms/agentstack/clients | \
  jq -r '.[].clientId'
```

### 8.2 Verificar la configuración del realm (p. ej. tras el fix 6.2)

```bash
curl -s -H "Authorization: Bearer $KC_TOKEN" \
  http://localhost:8336/admin/realms/agentstack | \
  jq '{accessTokenLifespan, ssoSessionIdleTimeout, ssoSessionMaxLifespan}'
```

### 8.3 Decodificar un context token (JWT)

El context token de la sección 4 es un JWT emitido por `agentstack-server`.
Decodificarlo muestra el modelo de seguridad completo: `iss`/`aud` (aquí vive
el problema del aud-mismatch que obligó al workaround PLATFORM_PUBLIC_HOST),
`context_id`, expiración y los permisos concedidos por scope.

```bash
echo $TOKEN | cut -d. -f2 | python3 -c "
import base64, sys, json
p = sys.stdin.read().strip(); p += '=' * (-len(p) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))"
```

---
