from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.scopes import McpConfiguredScopePolicy, ScopeRegistry, UpstreamSpecSecurityScopePolicy
from openapi_mcp_gateway.settings import OperationScopesConfig
from tests.operation import SHOP_OPERATIONS, operation


# The security schemes of a spec that takes an OAuth token, an OpenID Connect token, or HTTP basic.
SCHEMES = {
    'oauth': {'type': 'oauth2'},
    'oidc': {'type': 'openIdConnect'},
    'basic': {'type': 'http', 'scheme': 'basic'},
}

# A shop whose operations declare the upstream scopes they need, with tags for the MCP scopes beside them.
LIST_PRODUCTS = operation('listProducts', 'get', '/products', security=[{'oauth': ['read:products']}])
CREATE_PRODUCT = operation('createProduct', 'post', '/products', security=[{'oauth': ['write:products']}])
DELETE_PRODUCT = operation(
    'deleteProduct', 'delete', '/products/{productId}', security=[{'oauth': ['read:products', 'write:products']}]
)
SECURED_OPERATIONS = (LIST_PRODUCTS, CREATE_PRODUCT, DELETE_PRODUCT)

# The most a sign-in to the secured shop asks the upstream for.
SECURED_CEILING = ['read:products', 'write:products']


# Why a token whose upstream grant lacks what ``deleteProduct`` declares is refused.
DELETE_PRODUCT_UPSTREAM_REFUSAL = (
    'The upstream token lacks the scopes deleteProduct needs (read:products write:products). '
    'Add them to auth.upstream.scopes and have the authorization server grant them to this user, '
    'which under authorization_code takes signing in again, '
    'or correct its security under operations.deleteProduct.security.'
)


def configured_policy(**config: object) -> McpConfiguredScopePolicy:
    """A policy over the shop operations, configured as ``auth.mcp.operation_scopes`` would be."""
    return McpConfiguredScopePolicy(OperationScopesConfig.model_validate(config), SHOP_OPERATIONS, 'shop')


def operation_named(operation_id: str) -> OperationInfo:
    """The shop operation with ``operation_id``."""
    return next(candidate for candidate in SHOP_OPERATIONS if candidate.operation_id == operation_id)


def tag_scopes(*operations: OperationInfo) -> McpConfiguredScopePolicy:
    """MCP scopes derived from the tags of ``operations``, as ``auth.mcp.operation_scopes.auto: tags`` does."""
    return McpConfiguredScopePolicy(OperationScopesConfig(auto='tags'), operations, 'shop')


def secured_registry(scope_implies: dict[str, list[str]] | None = None) -> ScopeRegistry:
    """The secured shop registered under tag-derived MCP scopes, its upstream grant enforced by the spec."""
    scope_registry = ScopeRegistry(
        tag_scopes(*SECURED_OPERATIONS), UpstreamSpecSecurityScopePolicy(SCHEMES, scope_implies or {})
    )
    for secured_operation in SECURED_OPERATIONS:
        scope_registry.register(secured_operation.operation_id, secured_operation)
    return scope_registry
