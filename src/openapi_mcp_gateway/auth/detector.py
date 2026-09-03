import typing

import pydantic

from ..openapi import OpenAPISpec


class DetectedOAuthFlow(pydantic.BaseModel):
    """Minimal OAuth2 flow metadata describing how the gateway should obtain tokens.

    ``authorization_code`` and ``client_credentials`` come directly from the spec via ``detect_oauth_flows``.
    ``passthrough`` and ``resource_server`` are never produced from a spec,
    since ``securitySchemes`` can declare neither.
    The factory short-circuits to them only when ``auth.flow`` names them explicitly.
    """

    flow_type: typing.Literal['authorization_code', 'client_credentials', 'passthrough', 'resource_server']
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: dict[str, str] = pydantic.Field(default_factory=dict)


def detect_oauth_flows(spec: OpenAPISpec) -> list[DetectedOAuthFlow]:
    """Return every supported OAuth2 flow advertised under ``securitySchemes``."""
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
    """Pick one OAuth2 flow, preferring ``authorization_code``; ``None`` if the spec declares none."""
    flows = detect_oauth_flows(spec)
    if not flows:
        return None

    for flow in flows:
        if flow.flow_type == 'authorization_code':
            return flow
    return flows[0]


def detect_unsupported_oauth_flows(spec: OpenAPISpec) -> list[str]:
    """Return spec-declared OAuth2 flow names that the gateway does not implement.

    Only ``authorizationCode`` and ``clientCredentials`` are supported.
    ``password`` and ``implicit`` are deprecated by OAuth 2.1 and intentionally omitted.
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
