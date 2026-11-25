import typing

import pydantic

from ...openapi import OpenAPISpec


# The OAuth flows a spec's ``securitySchemes`` may declare that the gateway runs, by their OpenAPI name.
# OpenAPI defines a closed set of flows, so a new one arrives only with a new OpenAPI version.
# ``password`` and ``implicit`` are deprecated by OAuth 2.1 and intentionally left out.
_SPEC_FLOW_TYPES: dict[str, typing.Literal['authorization_code', 'client_credentials']] = {
    'authorizationCode': 'authorization_code',
    'clientCredentials': 'client_credentials',
}


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
        flows.extend(
            UpstreamOAuthFlow(
                flow_type=flow_type,
                authorization_url=oauth_flows[spec_name].get('authorizationUrl'),
                token_url=oauth_flows[spec_name]['tokenUrl'],
                scope_descriptions=oauth_flows[spec_name].get('scopes', {}),
            )
            for spec_name, flow_type in _SPEC_FLOW_TYPES.items()
            if spec_name in oauth_flows
        )
    return flows


def detect_unsupported_upstream_oauth_flows(spec: OpenAPISpec) -> list[str]:
    """Return spec-declared OAuth2 flow names that the gateway does not implement."""
    unsupported: list[str] = []
    for scheme in spec.security_schemes.values():
        if scheme.get('type') != 'oauth2':
            continue
        for flow_name in scheme.get('flows', {}):
            if flow_name not in _SPEC_FLOW_TYPES and flow_name not in unsupported:
                unsupported.append(flow_name)
    return unsupported
