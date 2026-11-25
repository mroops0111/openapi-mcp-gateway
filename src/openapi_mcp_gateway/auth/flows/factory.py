import dataclasses
import logging

from ...openapi import OpenAPISpec
from ...settings import ServerConfig
from ..assembly import AuthTypeContext, ServerAuthSetup
from ..upstream.oauth_flow import UpstreamOAuthFlow, detect_upstream_oauth_flows
from .authorization_code import AuthorizationCodeFlowHandler
from .client_credentials import UpstreamClientCredentialsFlowHandler
from .handler import OAuthFlowHandler
from .token_exchange import TokenExchangeFlowHandler


logger = logging.getLogger(__name__)


# The extension point for ``auth.upstream.flow``, in order of preference.
# A spec declaring several flows gets the first listed here, and a config without a spec flow the first it describes.
OAUTH_FLOW_HANDLERS: dict[str, type[OAuthFlowHandler]] = {
    'authorization_code': AuthorizationCodeFlowHandler,
    'client_credentials': UpstreamClientCredentialsFlowHandler,
    'token_exchange': TokenExchangeFlowHandler,
}


@dataclasses.dataclass(frozen=True)
class OAuthFlowSelection:
    """The OAuth flow settled for one server, before anything is built.

    ``handler`` is the one instance that both checks the config and builds the flow.
    """

    handler: OAuthFlowHandler
    flow_type: str
    upstream_oauth_flow: UpstreamOAuthFlow | None

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        """Build the flow this selection settled for ``context.entry``."""
        return self.handler.build(context, self.upstream_oauth_flow)


def select_oauth_flow(context: AuthTypeContext) -> OAuthFlowSelection:
    """Resolve the effective OAuth flow for ``context.entry`` and its handler, reading only the config and spec.

    A flow named in the config that reads no flow from the spec is taken as it is.
    Otherwise the resolution order is:

    1. Detect all OAuth flows declared in the spec.
    2. Pick one per ``auth.upstream.flow`` if set, otherwise prefer ``authorization_code``.
    3. If the spec declares none, synthesise from ``auth.upstream.{authorization_url, token_url, scopes}``.
    4. Apply config-supplied URL overrides on top of the spec-declared values.
    """
    entry = context.entry
    named_flow_type = entry.auth.upstream.flow
    named_handler_class = OAUTH_FLOW_HANDLERS.get(named_flow_type) if named_flow_type else None
    if named_flow_type and named_handler_class and not named_handler_class.reads_spec_oauth_flow:
        return OAuthFlowSelection(named_handler_class(), named_flow_type, None)
    upstream_oauth_flow = resolve_upstream_oauth_flow(entry, context.spec)
    handler_class = OAUTH_FLOW_HANDLERS.get(upstream_oauth_flow.flow_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{entry.name}": unsupported OAuth flow type "{upstream_oauth_flow.flow_type}". '
            f'Supported flows: {sorted(OAUTH_FLOW_HANDLERS)}'
        )
    return OAuthFlowSelection(handler_class(), upstream_oauth_flow.flow_type, upstream_oauth_flow)


def resolve_upstream_oauth_flow(entry: ServerConfig, spec: OpenAPISpec) -> UpstreamOAuthFlow:
    """Pick the effective OAuth flow for ``entry`` from the spec and config.

    Honours ``auth.upstream.flow`` as an explicit override.
    Otherwise it takes the declared flow that comes first in ``OAUTH_FLOW_HANDLERS``,
    and when the spec declares none, the first flow there whose handler ``synthesises_from`` the config.
    """
    explicit_flow_type = entry.auth.upstream.flow
    declared_flows = detect_upstream_oauth_flows(spec)
    selected_flow = _pick_from_declared_flows(declared_flows, explicit_flow_type)

    if selected_flow is None:
        # Spec declared no OAuth flows, so the flow must be synthesised from explicit config.
        selected_flow = _synthesise_from_config(entry, explicit_flow_type)

    if entry.auth.upstream.authorization_url:
        selected_flow.authorization_url = entry.auth.upstream.authorization_url
    if entry.auth.upstream.token_url:
        selected_flow.token_url = entry.auth.upstream.token_url

    logger.debug(
        'Resolved OAuth flow for "%s": flow=%s authorization_url=%s token_url=%s',
        entry.name,
        selected_flow.flow_type,
        selected_flow.authorization_url,
        selected_flow.token_url,
    )
    return selected_flow


def _pick_from_declared_flows(
    declared_flows: list[UpstreamOAuthFlow],
    explicit_flow_type: str | None,
) -> UpstreamOAuthFlow | None:
    """Return the spec-declared flow matching ``explicit_flow_type``, or the preferred default."""
    if not declared_flows:
        return None

    if explicit_flow_type is not None:
        for candidate in declared_flows:
            if candidate.flow_type == explicit_flow_type:
                return candidate
        raise ValueError(
            f'auth.upstream.flow="{explicit_flow_type}" but the spec does not declare that flow. '
            f'Declared flows: {[candidate.flow_type for candidate in declared_flows]}'
        )

    return min(declared_flows, key=lambda candidate: list(OAUTH_FLOW_HANDLERS).index(candidate.flow_type))


def _synthesise_from_config(entry: ServerConfig, explicit_flow_type: str | None) -> UpstreamOAuthFlow:
    """Build a ``UpstreamOAuthFlow`` from explicit config when the spec declares none."""
    if not entry.auth.upstream.token_url:
        raise ValueError(
            f'Server "{entry.name}": auth type is oauth2 but the spec has no OAuth2 flow and no token_url is provided. '
            'Set auth.upstream.token_url (and authorization_url if using authorization_code), or add a securitySchemes section to the spec.'
        )

    flow_type = explicit_flow_type or next(
        flow_type
        for flow_type, handler_class in OAUTH_FLOW_HANDLERS.items()
        if handler_class.synthesises_from(entry.auth.upstream)
    )

    return UpstreamOAuthFlow(
        flow_type=flow_type,  # type: ignore[arg-type]
        authorization_url=entry.auth.upstream.authorization_url,
        token_url=entry.auth.upstream.token_url,
        scope_descriptions=dict.fromkeys(entry.auth.upstream.scopes, ''),
    )
