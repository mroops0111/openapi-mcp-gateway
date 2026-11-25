import collections.abc
import dataclasses
import logging
import typing

from ..openapi import OpenAPISpec, OperationInfo
from ..settings import ServerConfig
from .policy import McpScopePolicy, UpstreamSpecSecurityScopePolicy, build_mcp_scope_policy, build_upstream_scope_policy
from .registry import ScopeRegistry


logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ServerScopeSetup:
    """What one server's scope settings settle against its spec, around the building of its auth.

    ``build`` runs before the auth is built, since the auth strategies read ``registry``.
    ``select_exposed_operations`` runs after, once the auth says whether a call carries a user's upstream grant.

    ``server_config`` is the config the auth is built from.
    Under ``enforce_scopes`` without ``auth.upstream.scopes``, it requests the upstream scopes the operations declare.
    """

    server_config: ServerConfig
    registry: ScopeRegistry
    spec_security: UpstreamSpecSecurityScopePolicy

    @classmethod
    def build(
        cls, server_config: ServerConfig, spec: OpenAPISpec, operations: collections.abc.Sequence[OperationInfo]
    ) -> typing.Self:
        """Settle the scopes of ``server_config``'s operations, refusing a client scope no operation defines."""
        mcp_scope_policy = build_mcp_scope_policy(
            server_config.auth.mcp.operation_scopes, operations, server_config.name
        )
        _refuse_undefined_mcp_scopes(server_config, mcp_scope_policy)
        upstream = server_config.auth.upstream
        spec_security = UpstreamSpecSecurityScopePolicy(spec.security_schemes, upstream.scope_implies)
        if upstream.enforce_scopes and not upstream.scopes:
            server_config = _with_requested_upstream_scopes(server_config, spec_security.declared_scopes(operations))
        registry = ScopeRegistry(mcp_scope_policy, build_upstream_scope_policy(upstream.enforce_scopes, spec_security))
        return cls(server_config, registry, spec_security)

    def select_exposed_operations(
        self, operations: list[OperationInfo], records_user_grant: bool
    ) -> list[OperationInfo]:
        """Return the operations to expose, given whether each call carries the user's own upstream grant.

        Raises ``ValueError`` when ``enforce_scopes`` leaves no operation any sign-in can open.
        Under ``enforce_scopes``, an operation no sign-in can open is left out, since every call to it is refused.
        Without it the gateway does not check the grant, so such an operation is only warned about.
        A flow that records no user grant never reads ``enforce_scopes``, so ``build_auth`` has refused it already.
        """
        server_config, spec_security = self.server_config, self.spec_security
        mcp_scope_policy = self.registry.mcp_policy
        upstream = server_config.auth.upstream
        if not records_user_grant:
            return operations
        if not upstream.enforce_scopes:
            spec_security.warn_on_unrequested_scopes(
                operations,
                [*upstream.required_scopes, *upstream.scopes],
                mcp_scope_policy,
                _scoped_clients(server_config),
                server_config.name,
            )
            return operations
        spec_security.warn_on_missing_oauth_scheme(server_config.name)
        exposed = spec_security.drop_unreachable_operations(
            operations, upstream.scopes, upstream.required_scopes, server_config.name
        )
        if not exposed:
            raise ValueError(
                f'Server "{server_config.name}": no operation is left to expose, '
                'since no sign-in can open any within auth.upstream.scopes and auth.upstream.required_scopes. '
                'Add the scopes the operations need to auth.upstream.scopes, '
                'or correct them under operations.<id>.security.'
            )
        spec_security.warn_on_unasked_ceiling_scopes(exposed, upstream.scopes, server_config.name)
        spec_security.warn_on_empty_sign_in(
            exposed, upstream.scopes, upstream.required_scopes, mcp_scope_policy, server_config.name
        )
        return exposed


def _refuse_undefined_mcp_scopes(server_config: ServerConfig, mcp_scope_policy: McpScopePolicy) -> None:
    """Refuse a client granted a scope the server does not define, since it would open nothing."""
    undefined = {
        client_id: sorted(set(scopes) - set(mcp_scope_policy.mcp_defined_scopes))
        for client_id, scopes in _scoped_clients(server_config).items()
    }
    named = '; '.join(f'{client_id}: {", ".join(scopes)}' for client_id, scopes in undefined.items() if scopes)
    if named:
        raise ValueError(
            f'Server "{server_config.name}": auth.mcp.clients or auth.mcp.other_clients grant scopes '
            f'auth.mcp.operation_scopes does not define ({named}).'
        )


def _with_requested_upstream_scopes(server_config: ServerConfig, scopes: list[str]) -> ServerConfig:
    """Return ``server_config`` requesting at most ``scopes`` upstream, as when ``auth.upstream.scopes`` lists them."""
    if scopes:
        logger.info(
            'Server "%s": auth.upstream.scopes is unset, so each sign-in asks for what it needs '
            'of the upstream scopes the exposed operations declare (%s).',
            server_config.name,
            ' '.join(scopes),
        )
    upstream = server_config.auth.upstream.model_copy(update={'scopes': scopes})
    return server_config.model_copy(update={'auth': server_config.auth.model_copy(update={'upstream': upstream})})


def _scoped_clients(server_config: ServerConfig) -> dict[str, list[str]]:
    """The scopes each configured client may hold, with unlisted metadata document clients as ``other_clients``."""
    mcp = server_config.auth.mcp
    scopes = {client_id: client.scopes for client_id, client in mcp.clients.items()}
    other_clients = mcp.resolve_other_clients()
    if mcp.operation_scopes is not None and other_clients is not None:
        scopes['other_clients'] = other_clients.scopes
    return scopes
