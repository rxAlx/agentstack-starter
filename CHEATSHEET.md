# A2A + AgentStack — Comandos clave

```
PLATFORM=http://localhost:8333
AUTH="-u admin:admin123"    # AgentStack 0.7.1 usa credenciales de Keycloak
```

---

## 1. Ver providers registrados (y sus IDs)

```bash
curl -s $AUTH $PLATFORM/api/v1/providers | \
  python3 -c "
import sys, json
for p in json.load(sys.stdin)['items']:
    print(p['state'], p['id'], p['agent_card']['name'])
"
```

Ejemplo de salida:

```
online  d55c2378-bd90-407e-1335-ca4176b0eaa4  llm_agent
online  3a1b9f02-cc11-4e22-8d12-fb3291a4c007  translator
online  2449af45-7304-1fbb-c0ca-d43197a3f09f  orchestrator
```

---

## 2. Ver la Agent Card de un agente

```bash
PROVIDER_ID=<id>

curl -s $AUTH \
  $PLATFORM/api/v1/a2a/$PROVIDER_ID/.well-known/agent-card.json | jq .
```

Devuelve: nombre, descripción, capacidades, extensiones requeridas, transportes.

---

## 3. Llamar al proxy OpenAI del platform (LLM directo)

```bash
curl -s $AUTH \
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
PROVIDER_ID=<id>

curl -s $AUTH \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/send",
    "id": "1",
    "params": {
      "message": {
        "messageId": "msg-001",
        "role": "user",
        "parts": [{"kind": "text", "text": "¿Cuál es la capital de Japón?"}],
        "metadata": {
          "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1": {
            "llm_fulfillments": {
              "default": {
                "api_base": "{platform_url}/api/v1/openai",
                "api_key": "x",
                "api_model": "openai:llama-3.1-8b-instant"
              }
            }
          }
        }
      }
    }
  }' \
  $PLATFORM/api/v1/a2a/$PROVIDER_ID | jq .
```

Extraer solo el texto de la respuesta del agente:

```bash
... | jq '[.result.history[] | select(.role=="agent") | .parts[].text] | join("")'
```

---

## 5. Arrancar los agentes

Ahora mismo los agentes los tengo edsplegados en local con "UV", debería crear imagenes docker y publicarlos en Git para desplegarlos via Kubernetes (más adelante)

```bash
cd agentstack-starter

# LLM agent (port 8000)
uv run --env-file .env server

# Translator agent (port 8001)
uv run --env-file .env.translator translator

# Orchestrator agent (port 8002)
uv run --env-file .env.orchestrator orchestrator
```

---

## Arquitectura de la metadata LLM (obligatoria en cada mensaje)

```
Los agentes que usan LLMServiceExtensionServer necesitan
que el CALLER inyecte esta metadata — el platform NO la inyecta.

{platform_url} es un placeholder que el SDK reemplaza
por la URL real del platform.
```

---

## 6. Port-forwards necesarios

```bash
kubectl port-forward svc/agentstack-server-svc 8333:8333 -n a2a &
kubectl port-forward svc/agentstack-ui-svc     8334:8334 -n a2a &
kubectl port-forward svc/keycloak              8336:8336 -n a2a &
```

---

## 7. Keycloak: restaurar token lifetime tras helm upgrade

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

## Flujo A2A completo

```
curl → platform:8333/api/v1/a2a/{id}
              │
              │  (platform proxy)
              ▼
        agente (192.168.49.1:800x)
              │
              │  LLMServiceExtension → api_base/chat/completions
              ▼
        platform:8333/api/v1/openai → Groq API
              │
              ▼
        respuesta (tokens en history[])
```
