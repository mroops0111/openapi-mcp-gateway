import typing

import pydantic

from ..openapi import OpenAPISpec


class DetectedOAuthFlow(pydantic.BaseModel):
    """Minimal OAuth2 flow metadata describing how the gateway should obtain tokens.

    ``authorization_code`` and ``client_credentials`` are produced directly by
    ``detect_oauth_flows`` when those flows are declared in the spec.
    ``passthrough`` is never produced from a spec — the factory selects it as
    a fallback when authorization_code is detected but the gateway lacks the
    upstream client credentials needed to act as an MCP-side OAuth server.
    """

    flow_type: typing.Literal['authorization_code', 'client_credentials', 'passthrough']
    authorization_url: str | None = None
    token_url: str | None = None
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


def detect_unsupported_oauth_flows(spec: OpenAPISpec) -> list[str]:
    """Return OAuth2 flow names declared by the spec that the gateway does not implement.

    Currently the gateway only implements ``authorizationCode`` and
    ``clientCredentials``; ``password`` and ``implicit`` are deprecated by the
    OAuth 2.1 working group and are intentionally not supported.
    """
    supported = {'authorizationCode', 'clientCredentials'}
    unsupported: list[str] = []
    for scheme in spec.security_schemes.values():
        if scheme.get('type') != 'oauth2':
            continue
        for flow_name in scheme.get('flows', {}):
            if flow_name not in supported and flow_name not in unsupported:
                unsupported.append(flow_name)
    return unsupported
