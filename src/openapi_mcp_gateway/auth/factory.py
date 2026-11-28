import dataclasses
import typing

from .assembly import (
    AuthTypeContext,
    McpSettingsReader,
    ServerAuthSetup,
    UpstreamDescription,
    UpstreamPrincipal,
)
from .authorities import MCP_TOKEN_AUTHORITIES, select_mcp_token_authority
from .flows import OAUTH_FLOW_HANDLERS
from .upstream_types import AUTH_TYPE_HANDLERS, UpstreamOAuth2Handler


# The ``auth.upstream.type`` that defers to ``auth.upstream.flow``, named where a setting every flow reads is refused.
_OAUTH2_TYPE = next(
    auth_type for auth_type, handler_class in AUTH_TYPE_HANDLERS.items() if handler_class is UpstreamOAuth2Handler
)


def build_auth(context: AuthTypeContext) -> ServerAuthSetup:
    """Assemble the auth components for one server, dispatching on ``auth.upstream.type``.

    Every strategy is selected first without any I/O, so a setting nothing reads is refused before anything is built.
    An upstream strategy that leaves the MCP half open gets one from ``MCP_TOKEN_AUTHORITIES``.
    """
    entry = context.entry
    auth_type = entry.auth.upstream.type
    handler_class = AUTH_TYPE_HANDLERS.get(auth_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{entry.name}": unsupported auth type "{auth_type}". Supported types: {sorted(AUTH_TYPE_HANDLERS)}'
        )
    handler = handler_class()
    selection = handler.select(context)
    _refuse_unread_upstream_settings(
        entry.name, entry.auth.upstream.model_fields_set - {'type'} - selection.upstream_settings
    )
    authority = (
        None
        if selection.mcp_settings_reader
        else select_mcp_token_authority(entry.auth, selection.upstream_principal, entry.name)
    )
    reader = selection.mcp_settings_reader or (authority if isinstance(authority, McpSettingsReader) else None)
    mcp_settings_read = reader.mcp_settings if reader else frozenset()
    _refuse_unread_mcp_settings(
        entry.name, entry.auth.mcp.model_fields_set - mcp_settings_read, selection.upstream_principal
    )
    if reader is not None:
        reader.refuse_unusable_settings(entry.auth, entry.name)

    setup = _with_credentials_described(handler.build(context, selection), entry.auth.upstream.credentials)
    return authority.attach(setup, context) if authority else setup


def _with_credentials_described(setup: ServerAuthSetup, credentials: typing.Collection[str]) -> ServerAuthSetup:
    """Name the static credentials' schemes beside what the flow reports, as ``credentials (<schemes>)``."""
    if not credentials:
        return setup
    schemes = ', '.join(credentials)
    described = f'credentials ({schemes})'
    description = setup.upstream_description
    return dataclasses.replace(
        setup,
        upstream_description=UpstreamDescription(
            described if description.summary == 'none' else f'{description.summary} + {described}',
            {**description.resolved_settings, 'credentials': schemes},
        ),
    )


def _refuse_unread_upstream_settings(server_name: str, unread: set[str]) -> None:
    """Refuse an ``auth.upstream`` field the selected strategy would never read, naming what would read it.

    Only explicitly-set fields are checked, so defaults never trip this.
    """
    if not unread:
        return
    field = sorted(unread)[0]
    flow_readers = [
        flow_type
        for flow_type, handler_class in OAUTH_FLOW_HANDLERS.items()
        if field in handler_class().upstream_settings
    ]
    type_readers = [
        f'auth.upstream.type {auth_type}'
        for auth_type, handler_class in AUTH_TYPE_HANDLERS.items()
        if field in handler_class().upstream_settings
    ]
    if flow_readers and set(flow_readers) == set(OAUTH_FLOW_HANDLERS):
        type_readers.append(f'auth.upstream.type {_OAUTH2_TYPE}')
    elif flow_readers:
        type_readers.append(f'auth.upstream.flow {" or ".join(flow_readers)}')
    requirement = ', or '.join(type_readers)
    raise ValueError(
        f'Server "{server_name}": auth.upstream.{field} needs {requirement}, so it would have no effect here.'
    )


def _refuse_unread_mcp_settings(server_name: str, unread: set[str], upstream_principal: UpstreamPrincipal) -> None:
    """Refuse an ``auth.mcp`` field the MCP half would never read, naming what would read it.

    Only explicitly-set fields are checked, so defaults never trip this.
    The suggestion prefers strategies for an upstream that sees the same identity, since those need no other change.
    """
    if not unread:
        return
    field = sorted(unread)[0]
    strategies = [
        *(handler_class() for handler_class in OAUTH_FLOW_HANDLERS.values()),
        *(authority_class() for authority_class in MCP_TOKEN_AUTHORITIES),
    ]
    readers = [
        strategy
        for strategy in strategies
        if isinstance(strategy, McpSettingsReader) and field in strategy.mcp_settings
    ]
    nearest = [reader for reader in readers if reader.upstream_principal == upstream_principal] or readers
    requirement = ', or '.join(reader.mcp_settings_requirement for reader in nearest)
    raise ValueError(f'Server "{server_name}": auth.mcp.{field} needs {requirement}, so it would have no effect here.')
