import abc
import typing

from ...settings import UpstreamAuthConfig
from ..assembly import AuthTypeContext, ServerAuthSetup, UpstreamPrincipal, UpstreamSetting
from ..upstream.oauth_flow import UpstreamOAuthFlow


# The ``auth.upstream`` fields that check each call against the user's own upstream grant.
# Only a flow that records such a grant reads them.
UPSTREAM_GRANT_CHECK_SETTINGS: frozenset[UpstreamSetting] = frozenset({'enforce_scopes', 'scope_implies'})


class OAuthFlowHandler(abc.ABC):
    """Strategy that assembles one ``auth.upstream.flow`` at startup.

    A flow whose two directions are independent builds only the upstream half.
    A flow that binds them builds both halves and the Bridge that joins them.
    Subclasses register in ``OAUTH_FLOW_HANDLERS``, so the factory can dispatch by ``flow_type``.
    A flow that builds the MCP half is also an ``McpSettingsReader``.
    """

    # Whether the flow runs one the spec declares against the upstream, as every flow does by default.
    # It is a fact of the class, so selecting a flow reads it without building a handler.
    reads_spec_oauth_flow: typing.ClassVar[bool] = True

    # Whether the flow records each user's own upstream grant, so a call can be checked against it.
    # Stated once here, so the settings that turn the check on follow from it,
    # and the grant source the flow builds is tested to agree.
    records_user_grant: typing.ClassVar[bool] = False

    @property
    @abc.abstractmethod
    def upstream_principal(self) -> UpstreamPrincipal:
        """Whose identity the upstream sees under this flow."""

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        """The ``auth.upstream`` fields this flow reads beyond ``flow``, its own and those its grant check needs."""
        return self.flow_settings | (UPSTREAM_GRANT_CHECK_SETTINGS if self.records_user_grant else frozenset())

    @property
    def flow_settings(self) -> frozenset[UpstreamSetting]:
        """The ``auth.upstream`` fields this flow reads for its own exchange with the upstream. None, by default."""
        return frozenset()

    @classmethod
    def synthesises_from(cls, config: UpstreamAuthConfig) -> bool:
        """Whether ``config`` alone describes this flow, for a spec that declares none. Never, by default."""
        return False

    @abc.abstractmethod
    def build(self, context: AuthTypeContext, upstream_oauth_flow: UpstreamOAuthFlow | None) -> ServerAuthSetup:
        """Return the upstream resolver and whatever MCP-side pieces and shutdown hook this flow brings.

        ``upstream_oauth_flow`` is the flow resolved from the spec and config,
        and ``None`` for a flow that reads none, as ``reads_spec_oauth_flow`` says.
        """


def require_upstream_oauth_flow(
    context: AuthTypeContext, upstream_oauth_flow: UpstreamOAuthFlow | None
) -> UpstreamOAuthFlow:
    """``upstream_oauth_flow``, which every flow reading one from the spec runs against the upstream."""
    if upstream_oauth_flow is None:
        raise ValueError(
            f'Server "{context.entry.name}": this flow needs an upstream OAuth flow, and none was resolved.'
        )
    return upstream_oauth_flow
