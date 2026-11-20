from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.scopes import (
    McpConfiguredScopePolicy,
)
from openapi_mcp_gateway.settings import OperationScopesConfig
from tests.operation import PETSTORE_OPERATIONS, operation


# The security schemes of a spec that takes an OAuth token, an OpenID Connect token, or HTTP basic.
SCHEMES = {
    'oauth': {'type': 'oauth2'},
    'oidc': {'type': 'openIdConnect'},
    'basic': {'type': 'http', 'scheme': 'basic'},
}

# A petstore operation that declares the upstream scopes it needs.
DELETE_PET = operation('deletePet', 'delete', '/pets/{petId}', security=[{'oauth': ['read:pets', 'write:pets']}])


# Why a token whose upstream grant lacks what ``deletePet`` declares is refused.
DELETE_PET_UPSTREAM_REFUSAL = (
    'The upstream token lacks the scopes deletePet needs (read:pets write:pets). '
    'Add them to auth.upstream.scopes and sign in again, or correct its security under operations.deletePet.security.'
)


def configured_policy(**config: object) -> McpConfiguredScopePolicy:
    """A policy over the petstore operations, configured as ``auth.mcp.operation_scopes`` would be."""
    return McpConfiguredScopePolicy(OperationScopesConfig.model_validate(config), PETSTORE_OPERATIONS, 'petstore')


def operation_named(operation_id: str) -> OperationInfo:
    """The petstore operation with ``operation_id``."""
    return next(candidate for candidate in PETSTORE_OPERATIONS if candidate.operation_id == operation_id)


def tag_scopes(*operations: OperationInfo) -> McpConfiguredScopePolicy:
    """MCP scopes derived from the tags of ``operations``, as ``auth.mcp.operation_scopes.auto: tags`` does."""
    return McpConfiguredScopePolicy(OperationScopesConfig(auto='tags'), operations, 'petstore')
