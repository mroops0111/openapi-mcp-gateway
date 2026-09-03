import logging

from ...openapi import OpenAPISpec
from ...settings import ServerConfig
from ...stores.base import TokenStore
from ..detector import DetectedOAuthFlow, detect_oauth_flows
from .authorization_code import AuthorizationCodeFlowHandler
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup
from .client_credentials import ClientCredentialsFlowHandler
from .passthrough import PassthroughFlowHandler
from .resource_server import ResourceServerFlowHandler


logger = logging.getLogger(__name__)


OAUTH_FLOW_HANDLERS: dict[str, type[OAuthFlowHandler]] = {
    'authorization_code': AuthorizationCodeFlowHandler,
    'client_credentials': ClientCredentialsFlowHandler,
    'passthrough': PassthroughFlowHandler,
    'resource_server': ResourceServerFlowHandler,
}


def build_oauth_flow(
    entry: ServerConfig,
    spec: OpenAPISpec,
    store: TokenStore,
    gateway_url: str,
    mount_path: str,
) -> OAuthFlowSetup:
    """Resolve the effective OAuth flow for ``entry`` and dispatch to its handler.

    Resolution order:

    1. Detect all OAuth flows declared in the spec.
    2. Pick one per ``entry.auth.flow`` if set, otherwise prefer ``authorization_code``.
    3. If the spec declares none, synthesise from ``entry.auth.{authorization_url, token_url, scopes}``.
    4. Apply config-supplied URL overrides on top of the spec-declared values.
    """
    oauth_flow = resolve_oauth_flow(entry, spec)
    handler_class = OAUTH_FLOW_HANDLERS.get(oauth_flow.flow_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{entry.name}": unsupported OAuth flow type "{oauth_flow.flow_type}". '
            f'Supported flows: {sorted(OAUTH_FLOW_HANDLERS)}'
        )

    flow_context = OAuthFlowContext(
        entry=entry,
        spec=spec,
        oauth_flow=oauth_flow,
        store=store,
        gateway_url=gateway_url,
        mount_path=mount_path,
    )
    return handler_class().build(flow_context)


def resolve_oauth_flow(entry: ServerConfig, spec: OpenAPISpec) -> DetectedOAuthFlow:
    """Pick the effective OAuth flow for ``entry`` from the spec and config combined.

    Honours ``entry.auth.flow`` as an explicit override,
    falls back to ``authorization_code`` when the spec declares both,
    and synthesises a flow from explicit URLs if the spec declares none.

    Raises ``ValueError`` when ``authorization_code`` is auto-selected,
    but the gateway lacks the ``client_id`` / ``client_secret`` needed to act as an MCP-side OAuth server.

    Passthrough is never auto-selected.
    Forwarding the MCP client's token to a third-party upstream violates RFC 8707 audience binding,
    the "confused deputy" pattern the MCP 2025-11-25 spec forbids.
    Set ``auth.flow: passthrough`` explicitly only when the gateway and the upstream share the same OAuth audience.
    """
    explicit_flow_type = entry.auth.flow

    # Neither can be declared in a spec, so the detector has nothing to contribute
    # and consulting it would only produce a misleading "flow not declared" error.
    if explicit_flow_type in ('passthrough', 'resource_server'):
        return DetectedOAuthFlow(flow_type=explicit_flow_type)

    declared_flows = detect_oauth_flows(spec)
    selected_flow = _pick_from_declared_flows(declared_flows, explicit_flow_type)

    if selected_flow is None:
        # Spec declared no OAuth flows, so the flow must be synthesised from explicit config.
        selected_flow = _synthesise_from_config(entry, explicit_flow_type)

    if entry.auth.authorization_url:
        selected_flow.authorization_url = entry.auth.authorization_url
    if entry.auth.token_url:
        selected_flow.token_url = entry.auth.token_url

    if (
        selected_flow.flow_type == 'authorization_code'
        and explicit_flow_type != 'authorization_code'
        and not (entry.auth.resolve_client_id() and entry.auth.resolve_client_secret())
    ):
        raise ValueError(
            f'Server "{entry.name}": authorization_code flow declared in spec but no client_id/client_secret configured.\n\n'
            'For third-party APIs (GitHub, Stripe, Asana, any external SaaS): '
            'you MUST register an OAuth app with the provider and set client_id/client_secret, '
            'so the gateway can mint its own upstream tokens. '
            "Forwarding the MCP client's token to a third-party upstream is forbidden by the MCP authorization spec "
            '(RFC 8707 audience binding).\n\n'
            'auth.flow: passthrough is ONLY for the rare case where the gateway and the upstream share the same OAuth audience, '
            'e.g. internal corporate SSO where both sides trust the same IdP-issued tokens, '
            'or the FastAPI integration where the gateway is mounted onto the app it exposes. '
            'It is NOT safe for third-party SaaS APIs.'
        )

    logger.debug(
        'Resolved OAuth flow for "%s": flow=%s authorization_url=%s token_url=%s',
        entry.name,
        selected_flow.flow_type,
        selected_flow.authorization_url,
        selected_flow.token_url,
    )
    return selected_flow


def _pick_from_declared_flows(
    declared_flows: list[DetectedOAuthFlow],
    explicit_flow_type: str | None,
) -> DetectedOAuthFlow | None:
    """Return the spec-declared flow matching ``explicit_flow_type``, or the preferred default."""
    if not declared_flows:
        return None

    if explicit_flow_type is not None:
        for candidate in declared_flows:
            if candidate.flow_type == explicit_flow_type:
                return candidate
        raise ValueError(
            f'auth.flow="{explicit_flow_type}" but the spec does not declare that flow. '
            f'Declared flows: {[f.flow_type for f in declared_flows]}'
        )

    for candidate in declared_flows:
        if candidate.flow_type == 'authorization_code':
            return candidate
    return declared_flows[0]


def _synthesise_from_config(entry: ServerConfig, explicit_flow_type: str | None) -> DetectedOAuthFlow:
    """Build a ``DetectedOAuthFlow`` from explicit config when the spec declares none."""
    if not entry.auth.token_url:
        raise ValueError(
            f'Server "{entry.name}": auth type is oauth2 but the spec has no OAuth2 flow '
            'and no token_url is provided. Set auth.token_url (and authorization_url if '
            'using authorization_code), or add a securitySchemes section to the spec.'
        )

    if explicit_flow_type is not None:
        flow_type = explicit_flow_type
    elif entry.auth.authorization_url:
        flow_type = 'authorization_code'
    else:
        flow_type = 'client_credentials'

    if flow_type == 'authorization_code' and not entry.auth.authorization_url:
        raise ValueError(f'Server "{entry.name}": authorization_code flow requires auth.authorization_url.')

    scopes_map = dict.fromkeys(entry.auth.scopes, '')
    return DetectedOAuthFlow(
        flow_type=flow_type,  # type: ignore[arg-type]
        authorization_url=entry.auth.authorization_url,
        token_url=entry.auth.token_url,
        scopes=scopes_map,
    )
