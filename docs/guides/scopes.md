---
description: Limit which operations each MCP client may list and call, and how much each user's sign-in asks the upstream for, under each auth mode.
---
# Scopes

Scopes limit which operations each MCP client may list and call, and how much each user's sign-in asks the upstream for. This guide covers their setup under each auth mode. [Scope Model](../concepts/scope-model.md) explains the rules behind them.

## Kinds of Scope

MCP scopes, the gateway's own from `auth.mcp.operation_scopes`, decide which operations a client may list and call, and upstream scopes, read from each operation's OpenAPI `security`, decide what the user's upstream token must hold under `auth.upstream.enforce_scopes`. [Scope Model](../concepts/scope-model.md#mechanisms-by-mode) compares the two and shows which auth modes support each.

## Defining MCP Scopes

`auth.mcp.operation_scopes` defines the MCP scopes and which operations each opens. Derive them from the spec's tags, write them by hand, or both. Where the gateway mints the tokens, this block pairs with the `clients` block in [Granting Scopes to Clients](#granting-scopes-to-clients):

```yaml
auth:
  mcp:
    operation_scopes:
      auto: tags
      unscoped: deny
      manual:
        pets:admin:
          description: Manage every pet
          operations: [createPet, deletePet]
```

- **`auto: tags`**: derives `{tag}:read` for GET and HEAD, `{tag}:write` for POST, PUT and PATCH, and `{tag}:delete` for DELETE. A tag is lowercased and its spaces become hyphens. So `pets:write` opens every POST, PUT and PATCH under the `pets` tag, and a `manual` scope opens a narrower set, such as one operation alone. `none` derives nothing.
- **`manual`**: each scope has a `description`, which the consent page shows, and `operations`, which takes the same patterns as `policy.allow`.
- **`unscoped`**: decides an operation no scope covers. `deny` closes it to every token, and `allow` opens it to every token.

[`auth.mcp`](../reference/configuration.md#authmcp) gives each key's default. Several scopes covering one operation add up, so holding any one is enough. With the config above and a spec whose operations carry the `pets` tag, the operations open as follows.

| Operation | Method | From `auto: tags` | From `manual` | Opens To |
| --- | --- | --- | --- | --- |
| `listPets` | GET | `pets:read` |  | `pets:read` |
| `createPet` | POST | `pets:write` | `pets:admin` | `pets:write` or `pets:admin` |
| `updatePet` | PUT | `pets:write` |  | `pets:write` |
| `deletePet` | DELETE | `pets:delete` | `pets:admin` | `pets:delete` or `pets:admin` |
| an operation with no tag |  |  |  | no token, under `unscoped: deny` |

> [!WARNING]
> With `unscoped: deny`, turning on `operation_scopes` closes every operation no scope covers. The gateway logs how many at startup.

## Granting Scopes to Clients

Where the gateway mints the tokens, each client receives only the scopes its entry allows. `scopes` is the most a client may hold, and `default_scopes` is what it receives when it asks for none:

```yaml
auth:
  mcp:
    operation_scopes:
      auto: tags
    clients:
      https://claude.ai/oauth/claude-code-client-metadata:
        scopes: [pets:read, pets:write]
        default_scopes: [pets:read]
    other_clients:
      trusted_hosts: [vscode.dev]
      scopes: [pets:read]
```

A sign-in is granted these scopes.

| Client Asks For | Granted |
| --- | --- |
| nothing | `default_scopes`, or all of `scopes` when unset |
| scopes all within `scopes` | exactly those |
| scopes partly within `scopes` | the part within |
| scopes none within `scopes` | refused with `invalid_scope` |

The grant follows the client, not the person. Everyone who signs in through the same client receives the same scopes. To limit individuals, rely on the upstream's own permissions, such as GitHub's repository roles, or use an external issuer that grants scopes per user, as [Scopes Under an External Issuer](#scopes-under-an-external-issuer) describes.

A client lists every tool its `scopes` could open, and a call its token lacks returns a `403` that the client steps up from by signing in again. [Step-Up](../concepts/scope-model.md#step-up) covers the challenge and what older clients get.

For agents on a shared credential, each agent needs a `client_secret`, and a request naming no scope receives all of `scopes`. `default_scopes`, `redirect_uris` and `consent` belong to a user's sign-in, so they are refused there. An agent steps up by requesting a new token with the wider `scope`.

A change to a client's `scopes` reaches its tokens only after it signs in again, as [Edge Cases](../concepts/scope-model.md#edge-cases) notes.

## Consent Page

Under `authorization_code`, the gateway shows a consent page during every MCP sign-in, before the upstream sign-in. It names the client and where access is sent, lists the MCP scopes with their descriptions, and lists the upstream host and the scopes the sign-in will ask for. A loopback redirect carries a warning that any program on the computer could have started the sign-in. A new upstream sign-in that a refused call starts, as [Upstream Token Refusals](../concepts/token-lifecycle.md#upstream-token-refusals) describes, goes straight to the upstream with no consent page.

- **Remembered**: an approval is kept per browser and client for 30 days, and the page is skipped while it covers both the MCP and the upstream scopes.
- **Shown Again**: when either side asks for more, with the added scopes marked "New".
- **Turned Off**: `auth.mcp.clients.<id>.consent: false` skips the page for that client. Clients admitted by `other_clients` always see the page.

## Scopes Under an External Issuer

Under `token_exchange` or a shared credential with `auth.mcp.issuer`, the issuer decides what each token holds. The gateway reads its `scope` or `scp` claim and checks it against `operation_scopes`.

```yaml
auth:
  mcp:
    issuer: https://auth.example.com/realms/internal
    required_scopes: [mcp]
    operation_scopes:
      auto: tags
```

- **`required_scopes`**: required. Every token must carry these scopes, and the protected resource metadata lists them as `scopes_supported`, which is what a client asks the issuer for first. Without it the MCP SDK asks for every scope the issuer advertises, which the issuer refuses for any client it restricts.
- **Visibility and Step-Up**: every tool a defined scope opens stays listed, and a call lacking its scope steps up at the issuer, as [Visibility](../concepts/scope-model.md#visibility) explains.
- **Exchange Refusal**: under `token_exchange` with `enforce_scopes`, a call to an operation that declares upstream scopes exchanges the caller's token before it runs. When the issuer refuses that exchange with a `400` `invalid_scope` or `invalid_target`, as Keycloak does for a scope the gateway's client may not hold, the call's error names the scopes asked for and the error code. Any other failed exchange is logged, and the call is told only that the gateway could not obtain its upstream credential.

`clients` and `other_clients` are refused at startup here, since the issuer grants the scopes.

## Asking the Upstream for Less

`auth.upstream.enforce_scopes` makes each sign-in or exchange ask the upstream for only what its MCP scopes open. It also checks every call against the upstream grant. It applies under `authorization_code` and `token_exchange`, and reads the upstream scopes each operation needs from its OpenAPI `security`.

```yaml
auth:
  upstream:
    type: oauth2
    flow: authorization_code
    client_id: ${PETS_CLIENT_ID}
    client_secret: ${PETS_CLIENT_SECRET}
    enforce_scopes: true
    scopes: [read:pets, write:pets, admin]
    required_scopes: [offline_access]
    scope_implies:
      admin: [read:pets, write:pets]
```

- **`scopes`**: the ceiling, the most a sign-in asks for. Unset under `enforce_scopes`, it is every scope the exposed operations declare.
- **`required_scopes`**: asked for at every sign-in, first. Put `offline_access` and `openid` here, since no operation declares them and downscoping never asks for them otherwise.
- **Upstream Grant Check**: every call is also checked against what the upstream actually granted, as [Permission Check](../concepts/scope-model.md#permission-check) describes.

With the config above, each MCP grant asks the upstream for the following. [Downscoping](../concepts/scope-model.md#downscoping) gives the steps that choose each request.

| MCP Grant | Asked Upstream |
| --- | --- |
| `pets:read` | `offline_access read:pets` |
| `pets:read pets:write` | `offline_access read:pets write:pets` |
| none | `offline_access` |

Without `enforce_scopes`, every sign-in or exchange asks for `required_scopes` plus all of `scopes`, and no call is checked against the upstream grant.

## Fixing an Imprecise Spec

`enforce_scopes` trusts the spec's `security`, so a spec that declares scopes wrongly leads to wrong verdicts. Two remedies fix one without turning the check off. Both are server keys, set beside the `auth.upstream` block above:

```yaml
operations:
  createPet:
    security:
      - oauth: [write:pets]
auth:
  upstream:
    scope_implies:
      admin: [read:pets, write:pets]
```

- **`operations.<operationId>.security`**: replaces one operation's `security`, in the same OpenAPI form. It takes an operationId only, no pattern, and leaves `x-mcp-integration` as it is. Each scheme it names, such as `oauth` here, must be one the spec declares under `components.securitySchemes`.
- **`auth.upstream.scope_implies`**: maps an upstream scope to the narrower ones it covers, such as `admin` covering `read:pets` and `write:pets` above. It applies transitively. Take it from the upstream's documentation, never from scope names.

A spec with no `security` at all skips the upstream grant check, and the upstream refuses a short token itself.

## Troubleshooting

Startup refuses a config that cannot work and warns where the verdict rests on the spec. Each message names the key to change.

| Message Says | Fix |
| --- | --- |
| `operation_scopes` needs `clients` or `other_clients` | list the clients the gateway grants scopes to |
| `required_scopes` cannot be combined with `operation_scopes` | remove `auth.mcp.required_scopes` where the gateway mints tokens |
| `auth.mcp.issuer` needs `auth.mcp.required_scopes` | add the scopes every client asks the issuer for first |
| clients grant scopes but `operation_scopes` defines none | define `operation_scopes`, or drop the client `scopes` |
| `default_scopes` names scopes not in its `scopes` | add them to `scopes`, or remove them from `default_scopes` |
| `enforce_scopes` or `scope_implies` needs `authorization_code` or `token_exchange` | remove it, since a shared credential has no per-user grant |
| an operation no sign-in can open is left out | widen `auth.upstream.scopes`, or fix the spec with the remedies above |
| a sign-in asks the upstream for no scope | add what the upstream needs to `auth.upstream.required_scopes` |
| ceiling scopes no operation declares | move them, such as `offline_access`, to `auth.upstream.required_scopes` |
| operations no MCP scope covers | cover them with a scope, or set `unscoped: allow` on purpose |

A client that keeps getting `403` after stepping up may not be allowed the scope. The challenge then names no scope and says so. Widen that client's `scopes`, or have the issuer grant it.

## Related

Scopes build on the auth mode a server uses, and the scope model explains the rules this setup follows.

- **Guides**: [Authorization](authorization.md) for the modes that scopes build on
- **Concepts**: [Scope Model](../concepts/scope-model.md) for the permission check, visibility and step-up
- **Reference**: [`auth.mcp`](../reference/configuration.md#authmcp) for `operation_scopes` and `clients` and [`auth.upstream`](../reference/configuration.md#authupstream) for `enforce_scopes` and `scope_implies`
