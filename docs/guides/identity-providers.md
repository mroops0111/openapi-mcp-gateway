---
description: What an identity provider must support for token exchange or an external issuer, with the Keycloak setup the gateway is tested against.
---
# Identity Providers

Under `token_exchange`, or a shared credential with `auth.mcp.issuer`, your identity provider mints the MCP endpoint's tokens. This guide covers what the identity provider must support, with Keycloak, the one identity provider the gateway is tested against, as an example.

## Provider Requirements

`token_exchange` needs an identity provider that implements [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693) token exchange. Whatever the identity provider, its configuration must meet four requirements, and each one prevents a specific failure.

- **Endpoint in the Audience**: every token a caller presents must carry the MCP endpoint's URI in `aud`, or the gateway refuses it.
- **Exchanging Client in the Audience**: the subject token is the caller's token, which the gateway hands to the identity provider in the exchange. The client the gateway exchanges as must also be in its audience, or the identity provider refuses the exchange. Naming that client by the endpoint URI meets both requirements at once.
- **Upstream as a Target**: the identity provider must know the upstream's audience as a target it may mint tokens for, or the exchange fails.
- **Allowed Scopes**: the identity provider must allow the exchanging client the upstream scopes it asks for, and each caller the MCP scopes it may step up to. [Scopes Under an External Issuer](scopes.md#scopes-under-an-external-issuer) states when a refusal reaches the call with its error code.

The gateway is tested end to end against Keycloak 26.2 only, and [Keycloak Example](#keycloak-example) shows these requirements in Keycloak's terms. Other identity providers are untested, so check their token exchange documentation before choosing this mode.

A shared credential with `auth.mcp.issuer` exchanges nothing. It needs only the first requirement and the MCP scopes from the last.

## Client Registration

MCP clients register with the identity provider, not the gateway, since the identity provider is the authorization server. The spec prefers pre-registration, then metadata document clients, and deprecates Dynamic Client Registration. [Client Registration](authorization.md#client-registration) defines all three. Every identity provider supports pre-registration, so check which of the other two yours supports before relying on them.

## Keycloak Example

This is the reference setup the gateway's [Keycloak tests](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/tests/e2e/keycloak.py) run against Keycloak 26.2. It meets the four [Provider Requirements](#provider-requirements) in Keycloak's terms, and assumes an MCP endpoint at `https://gw.example.com/pets/mcp` and an upstream API whose audience is `pets-api`.

### Gateway Config

The gateway exchanges tokens as a Keycloak client named by its endpoint's URI.

```yaml
url: https://gw.example.com
servers:
  - name: pets
    spec: https://pets.example.com/openapi.json
    auth:
      upstream:
        type: oauth2
        flow: token_exchange
        audience: pets-api
        client_id: https://gw.example.com/pets/mcp
        client_secret: ${GATEWAY_CLIENT_SECRET}
        scopes: [read:pets, write:pets]
        enforce_scopes: true
      mcp:
        issuer: https://keycloak.example.com/realms/pets
        required_scopes: [pets:read]
        operation_scopes:
          auto: tags
```

### Client Scopes

Each client scope below has "Include in token scope" on, so its name appears in the token's `scope`.

| Client Scope | Mapper | Purpose |
| --- | --- | --- |
| `mcp-audience` | Audience, included client `https://gw.example.com/pets/mcp`, added to the access token | puts the MCP endpoint in every caller's `aud` |
| each MCP scope, such as `pets:read` | none | the scopes `operation_scopes` checks |
| each upstream scope, such as `read:pets` | Audience, included client `pets-api`, added to the access token | the scopes an exchange asks for, each naming the upstream |

Keycloak ignores `resource` and `audience` on its authorization and token endpoints. Without the `mcp-audience` mapper a token's audience is only `account`, and the gateway refuses it.

### Clients

Three kinds of client make up the realm. A confidential client is one that proves its identity with its own secret.

| Client | Settings |
| --- | --- |
| `https://gw.example.com/pets/mcp`, the gateway | confidential, Standard Token Exchange on, optional client scopes `read:pets` and `write:pets` |
| `pets-api`, the upstream | confidential, the target an exchange names |
| each caller, such as a machine client | confidential, service account on, default client scope `mcp-audience`, optional client scopes are the MCP scopes it may hold |

Each setting maps to a requirement, and shows the error Keycloak returns without it.

- **Gateway in the Audience**: the exchanging client must be in the subject token's audience, or the exchange fails with `access_denied: Client is not within the token audience`. Naming the gateway's client by the endpoint URI makes it both the audience and the exchanging client.
- **Upstream Client Exists**: the exchange target must exist as a client and be reachable from the gateway client's scopes, or the exchange fails with `invalid_request: Requested audience not available`.
- **Optional Upstream Scopes**: the gateway client may exchange only for the upstream scopes it lists. Keycloak refuses an exchange for any other with `invalid_scope`, which reaches the call as [Scopes Under an External Issuer](scopes.md#scopes-under-an-external-issuer) states.
- **Optional MCP Scopes**: a caller steps up by asking for another of its optional scopes. Keycloak refuses one it does not list.

## Related

The authorization guide covers the modes that rely on an identity provider, and the reference lists the fields that name it.

- **Guides**: [Authorization](authorization.md#token-exchange) for the `token_exchange` setup the identity provider serves
- **Concepts**: [Scope Model](../concepts/scope-model.md#step-up) for how a caller steps up to a scope the issuer grants
- **Reference**: [`auth.upstream`](../reference/configuration.md#authupstream) for the exchange's client, audience and scopes and [`auth.mcp`](../reference/configuration.md#authmcp) for `issuer` and `required_scopes`
