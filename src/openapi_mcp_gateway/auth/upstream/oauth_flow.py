import typing

import pydantic

from ...openapi import OpenAPISpec


class UpstreamOAuthFlow(pydantic.BaseModel):
    """The OAuth flow the gateway runs against the upstream authorization server, and where that server is.

    It comes from the spec's ``securitySchemes`` when they declare one, and from ``auth.upstream`` otherwise.
    ``token_exchange`` has none, since it asks the issuer of the caller's token rather than the upstream's server.
    ``scope_descriptions`` maps each upstream scope to what it allows, as the spec states it.
    """

    flow_type: typing.Literal['authorization_code', 'client_credentials']
    authorization_url: str | None = None
    token_url: str | None = None
    scope_descriptions: dict[str, str] = pydantic.Field(default_factory=dict)


def detect_upstream_oauth_flows(spec: OpenAPISpec) -> list[UpstreamOAuthFlow]:
    """Return every supported OAuth2 flow advertised under ``securitySchemes``."""
    flows: list[UpstreamOAuthFlow] = []

    for _scheme_name, scheme in spec.security_schemes.items():
        if scheme.get('type') != 'oauth2':
            continue

        oauth_flows = scheme.get('flows', {})

        if 'authorizationCode' in oauth_flows:
            flow_data = oauth_flows['authorizationCode']
            flows.append(
                UpstreamOAuthFlow(
                    flow_type='authorization_code',
                    authorization_url=flow_data.get('authorizationUrl'),
                    token_url=flow_data['tokenUrl'],
                    scope_descriptions=flow_data.get('scopes', {}),
                )
            )

        if 'clientCredentials' in oauth_flows:
            flow_data = oauth_flows['clientCredentials']
            flows.append(
                UpstreamOAuthFlow(
                    flow_type='client_credentials',
                    token_url=flow_data['tokenUrl'],
                    scope_descriptions=flow_data.get('scopes', {}),
                )
            )

    return flows


def detect_primary_upstream_oauth_flow(spec: OpenAPISpec) -> UpstreamOAuthFlow | None:
    """Pick one OAuth2 flow, preferring ``authorization_code``, or return ``None`` if the spec declares none."""
    flows = detect_upstream_oauth_flows(spec)
    if not flows:
        return None

    for flow in flows:
        if flow.flow_type == 'authorization_code':
            return flow
    return flows[0]


def detect_unsupported_upstream_oauth_flows(spec: OpenAPISpec) -> list[str]:
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
