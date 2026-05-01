import typing

import pydantic

from ..openapi import OpenAPISpec


class DetectedOAuthFlow(pydantic.BaseModel):
    """Minimal OAuth2 flow metadata extracted from ``components.securitySchemes``."""

    flow_type: typing.Literal['authorization_code', 'client_credentials']
    authorization_url: str | None = None
    token_url: str
    scopes: dict[str, str] = pydantic.Field(default_factory=dict)


def detect_oauth_flows(spec: OpenAPISpec) -> list[DetectedOAuthFlow]:
    """Return every OAuth2 flow advertised under ``securitySchemes``."""
    flows: list[DetectedOAuthFlow] = []

    for _scheme_name, scheme in spec.security_schemes.items():
        if scheme.get('type') != 'oauth2':
            continue

        oauth_flows = scheme.get('flows', {})

        if 'authorizationCode' in oauth_flows:
            flow_data = oauth_flows['authorizationCode']
            flows.append(
                DetectedOAuthFlow(
                    flow_type='authorization_code',
                    authorization_url=flow_data.get('authorizationUrl'),
                    token_url=flow_data['tokenUrl'],
                    scopes=flow_data.get('scopes', {}),
                )
            )

        if 'clientCredentials' in oauth_flows:
            flow_data = oauth_flows['clientCredentials']
            flows.append(
                DetectedOAuthFlow(
                    flow_type='client_credentials',
                    token_url=flow_data['tokenUrl'],
                    scopes=flow_data.get('scopes', {}),
                )
            )

    return flows


def detect_primary_oauth_flow(spec: OpenAPISpec) -> DetectedOAuthFlow | None:
    """Pick a single OAuth2 flow, favouring ``authorization_code``.

    Returns ``None`` when the document defines no OAuth2 flows.
    """
    flows = detect_oauth_flows(spec)
    if not flows:
        return None

    # Prefer authorization_code
    for flow in flows:
        if flow.flow_type == 'authorization_code':
            return flow
    return flows[0]
