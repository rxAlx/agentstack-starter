#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A2A vs FIPA-XMPP Protocol Analyzer & Dumper
===========================================
Este script genera y muestra los mensajes nativos en formato JSON-RPC del protocolo A2A,
utilizando la librería oficial 'a2a' de Agent Stack para la construcción de objetos,
facilitando una comparación directa entre A2A-JSON (HTTP) y FIPA-XMPP (XML).

Autor: Antigravity
Fecha: 16 de Julio de 2026
"""

import sys
import json
import uuid
import asyncio
import subprocess
import os

import httpx
from a2a.types import (
    SendMessageRequest,
    SendMessageSuccessResponse,
    Message,
    TextPart,
    Role,
    Task,
    TaskStatus,
    TaskState
)

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8333")
ADMIN_USER = os.getenv("AGENTSTACK_ADMIN_USER", "admin")

# =============================================================================
# GENERADORES DE MENSAJES NATIVOS DE A2A UTILIZANDO LA LIBRERÍA 'A2A'
# =============================================================================

def build_native_a2a_request(text: str, context_id: str | None = None) -> SendMessageRequest:
    """Construye una petición nativa de envío de mensaje utilizando los modelos de la librería a2a."""
    msg_id = str(uuid.uuid4())
    ctx_id = context_id or str(uuid.uuid4())
    
    part = TextPart(kind="text", text=text, metadata=None)
    
    a2a_msg = Message(
        message_id=msg_id,
        context_id=ctx_id,
        role=Role.user,
        kind="message",
        parts=[part],
        metadata={}
    )
    
    return SendMessageRequest(
        jsonrpc="2.0",
        method="message/send",
        id=str(uuid.uuid4()),
        params={"message": a2a_msg}
    )


def build_native_a2a_response(request_payload: SendMessageRequest, response_text: str) -> SendMessageSuccessResponse:
    """Construye un objeto SendMessageSuccessResponse utilizando los modelos de la librería a2a."""
    req_msg = request_payload.params.message
    task_id = str(uuid.uuid4())
    
    res_part = TextPart(kind="text", text=response_text, metadata=None)
    
    res_msg = Message(
        message_id=str(uuid.uuid4()),
        context_id=req_msg.context_id,
        taskId=task_id,
        role=Role.agent,
        kind="message",
        parts=[res_part],
        metadata={}
    )
    
    task = Task(
        id=task_id,
        context_id=req_msg.context_id,
        kind="task",
        status=TaskStatus(state=TaskState.completed, message=None, error=None),
        history=[req_msg, res_msg],
        metadata={}
    )
    
    return SendMessageSuccessResponse(
        jsonrpc="2.0",
        id=request_payload.id,
        result=task
    )


# =============================================================================
# SECCIÓN DE VISUALIZACIÓN Y COMPARATIVA
# =============================================================================

def print_title(title):
    print("\n" + "=" * 80)
    print(f" {title.center(78)} ")
    print("=" * 80)

# =============================================================================
# SIMULACIÓN LIVE (CONEXIÓN A AGENT STACK LOCAL)
# =============================================================================

def _admin_password() -> str:
    pw = os.getenv("AGENTSTACK_ADMIN_PASSWORD")
    if pw:
        return pw
    try:
        out = subprocess.run(
            ["kubectl", "get", "secret", "agentstack-agents-secret", "-n", "a2a",
             "-o", "jsonpath={.data.admin-password}"],
            capture_output=True, text=True, check=True,
        )
        import base64
        return base64.b64decode(out.stdout).decode()
    except Exception:
        return "my-secret-password"


async def run_live_simulation():
    print_title("SIMULACIÓN EN VIVO: CAPTURA DE MENSAJES JSON-RPC EN AGENT STACK")
    
    password = _admin_password()
    print(f"[Info] Conectando a: {PLATFORM_URL}")
    
    async with httpx.AsyncClient(auth=(ADMIN_USER, password), timeout=30) as admin:
        try:
            # Configurar contexto
            resp = await admin.post(f"{PLATFORM_URL}/api/v1/contexts", json={})
            resp.raise_for_status()
            context_id = resp.json()["id"]
            
            # Token
            resp = await admin.post(
                f"{PLATFORM_URL}/api/v1/contexts/{context_id}/token",
                json={
                    "grant_global_permissions": {"llm": ["*"], "a2a_proxy": ["*"]},
                    "grant_context_permissions": {"files": ["*"], "vector_stores": ["*"]},
                },
            )
            resp.raise_for_status()
            token = resp.json()["token"]
            
            # Modelo
            resp = await admin.post(
                f"{PLATFORM_URL}/api/v1/model_providers/match",
                json={"capability": "llm", "suggested_models": []},
            )
            resp.raise_for_status()
            model_id = resp.json()["items"][0]["model_id"]
            
            # Descubrir agentes
            resp = await admin.get(f"{PLATFORM_URL}/api/v1/providers")
            resp.raise_for_status()
            
            agents = {}
            for item in resp.json().get("items", []):
                card = item.get("agent_card") or {}
                name = card.get("name")
                if item.get("state") == "online":
                    agents[name] = f"{PLATFORM_URL}/api/v1/a2a/{item['id']}/"
            
            target_agent = "translator" if "translator" in agents else (list(agents.keys())[0] if agents else None)
            if not target_agent:
                print("[Error] No hay agentes online.")
                return
                
            print(f"[Info] Agente de destino: {target_agent} ({agents[target_agent]})")
            
        except Exception as e:
            print(f"\n[Error de plataforma] {e}")
            return

        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as client:
            llm_metadata = {
                "https://a2a-extensions.agentstack.beeai.dev/services/llm/v1": {
                    "llm_fulfillments": {
                        "default": {
                            "api_base": os.getenv("LLM_API_BASE_INTERNAL", "http://agentstack-server-svc:8333/api/v1/openai"),
                            "api_key": token,
                            "api_model": model_id,
                        }
                    }
                }
            }
            
            # 1. Construir petición A2A real
            req_payload = build_native_a2a_request(
                "Please translate: The stars look wonderful tonight.",
                context_id=context_id
            )
            # Inyectar metadata LLM requerida por Agent Stack
            req_payload.params.message.metadata.update(llm_metadata)
            
            # Mostrar la petición en consola
            print_req = json.loads(req_payload.model_dump_json())
            # Redactar llave
            print_req["params"]["message"]["metadata"]["https://a2a-extensions.agentstack.beeai.dev/services/llm/v1"]["llm_fulfillments"]["default"]["api_key"] = "<redacted>"
            
            print("\n>>> ENVIANDO MENSAJE A2A (JSON-RPC Request):")
            print(json.dumps(print_req, indent=2, ensure_ascii=False))
            
            # Enviar usando dict json
            resp = await client.post(agents[target_agent], json=json.loads(req_payload.model_dump_json()))
            resp.raise_for_status()
            
            # Recibir respuesta
            result = resp.json()
            print("\n<<< RECIBIENDO RESPUESTA A2A (JSON-RPC Response):")
            print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run_live_simulation())
