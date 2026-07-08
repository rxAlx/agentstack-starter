"""
Shared security declaration for all agent cards (A2A `securitySchemes` / `security`).

This is DECLARATIVE: it tells clients how to authenticate at the official
door (the platform A2A proxy); enforcement happens at that proxy, not in
the agents themselves.
"""

import os

from a2a.types import HTTPAuthSecurityScheme, OpenIdConnectSecurityScheme, SecurityScheme

KEYCLOAK_OIDC_URL = os.getenv(
    "KEYCLOAK_OIDC_URL",
    "http://localhost:8336/realms/agentstack/.well-known/openid-configuration",
)

SECURITY_SCHEMES = {
    "contextToken": SecurityScheme(
        root=HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="JWT",
            description=(
                "AgentStack context token issued via "
                "POST /api/v1/contexts/{id}/token; send as 'Authorization: Bearer <token>' "
                "to the platform A2A proxy."
            ),
        )
    ),
    "keycloakOpenId": SecurityScheme(
        root=OpenIdConnectSecurityScheme(
            open_id_connect_url=KEYCLOAK_OIDC_URL,
            description="Platform identity provider (Keycloak realm 'agentstack').",
        )
    ),
}

SECURITY = [{"contextToken": []}, {"keycloakOpenId": []}]
