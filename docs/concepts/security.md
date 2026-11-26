---
description: The four access control layers the gateway adds in front of an API, how they compare with common setups, and its built-in safeguards.
---
# Security

This page explains the fine-grained access control the gateway adds in front of an API. Each caller reaches only the operations its task needs, with a credential no wider than those operations, which a server sharing one API key cannot offer.

## Access Control Layers

The gateway controls access at four layers, from the endpoint down to the credential the API receives. Each layer narrows what the one around it allowed.

![Four nested access layers. The endpoint layer decides who may call at all. Inside it, the client layer decides which applications may sign in. Inside that, the operation layer decides which tools each client may use. Innermost, the upstream credential layer decides what the API token may do.](../assets/access-layers-light.svg#only-light){ width="560" }
![The four nested access layers, as described above.](../assets/access-layers-dark.svg#only-dark){ width="560" }

| Layer | Question It Answers | Control | Set Up In |
| --- | --- | --- | --- |
| Endpoint | Who may call at all? | the gateway's own sign-in, your identity provider, or registered agents | [Mode Selection](../guides/authorization.md#mode-selection) |
| Client | Which applications may sign in? | pre-registered clients, and metadata document clients by host | [Client Registration](../guides/authorization.md#client-registration) |
| Operation | Which tools may each client list and call? | operation scopes, such as read, write and delete per tag, with uncovered operations closed by default | [Scopes](../guides/scopes.md) |
| Upstream Credential | What may the API token itself do? | each user's own token, asked with `enforce_scopes` for only the scopes the client's operations need | [Asking the Upstream for Less](../guides/scopes.md#asking-the-upstream-for-less) |

## Comparison With Common Setups

Two setups are common when an API is put behind MCP. A server holds one API key for every caller, or it passes each caller's own token through to the API, which the MCP specification calls token passthrough and forbids. The table compares both with the gateway under a per-user mode and operation scopes.

<div class="comparison" markdown>

| Concern | The Gateway | One Shared Key | Token Passthrough |
| --- | --- | --- | --- |
| Caller Identity | each client signs in, or your identity provider vouches for it | anyone who can reach the URL | the caller's own API token |
| Credential at the API | each user's own token, obtained by the gateway | the same key for every caller | the caller's token as is |
| Tool Access | per operation and per client | every tool for every caller | every tool, limited only by what the API refuses |
| Upstream Permission | only what the client's operations need, with `enforce_scopes` | everything the key allows | everything the caller's token allows |
| Needing More Access | a step-up during the call, with the user's consent | a config change on the server | a new token issued by hand |
| Removing a Caller | removing that client, or revoking it at the identity provider | rotating the key for everyone | revoking that token at the API |

</div>

[Scope Model](scope-model.md) states the rules behind the operation and upstream layers.

## Built-In Safeguards

Beneath the four layers, the gateway follows the MCP specification's security rules with no configuration.

- **Token Not Relayed**: a client's token never reaches a separate API, as [Token Forwarding Policy](architecture.md#token-forwarding-policy) explains.
- **Bound Tokens**: every token works only at the endpoint and server that minted it, as [Token Audience](token-lifecycle.md#token-audience) states.
- **Safe Metadata Fetch**: a client's metadata document is fetched from a public `https` address only, within size and time limits, as [Client Registration](../guides/authorization.md#client-registration) states.
- **Consent**: the user sees which client asks, where access is sent, and what it may do, on the [consent page](../guides/scopes.md#consent-page).
- **Issuer Check**: with `auth.upstream.issuer` set, an upstream response that carries `iss` must name it, which stops an authorization server mix-up, as [Per-User Sign-In](../guides/authorization.md#per-user-sign-in) states.
- **Browser Protection**: on a loopback bind, `Host` and `Origin` checks stop a web page from reaching a local gateway. The default bind is not loopback, so it accepts any `Host` until `dns_rebinding_protection.allowed_hosts` is set, as [Host and Origin Checks](../guides/deployment.md#host-and-origin-checks) states.
- **Fail-Closed Config**: an unknown key is refused at startup, and credentials can come from environment variables, as [Values and Validation](../reference/configuration.md#values-and-validation) states.
- **Model-Proof Limits**: policy, operation scopes and hidden parameters hold whatever a prompt asks, as [Features Outside Evaluation](../evaluation.md#features-outside-evaluation) notes.

## Out of Scope

The gateway leaves these to other parts of a deployment.

- **TLS**: the gateway serves plain HTTP, so a reverse proxy in front terminates TLS.
- **Upstream Authorization**: the upstream decides what its credential may do. Under a shared credential every caller reaches it as one identity, and an upstream refusal reaches the client unchanged.
- **External Issuer Revocation**: a token the issuer revokes stays valid until it expires, as [Revocation](token-lifecycle.md#revocation) explains, so keep token lifetimes short at the issuer.
- **The MCP Client**: the client holds its tokens, and a loopback redirect reaches any program on the computer, which the consent page warns about.
- **Response Content**: an upstream response reaches the model as the upstream sent it, reshaped only by `response`, so instructions planted in its data are not filtered.

## Related

The auth mode decides which of these defenses apply, and a few deployment settings need checking before the gateway is reachable from a network.

- **Guides**: [Authorization](../guides/authorization.md) for choosing a mode and [Security Notes](../guides/deployment.md#security-notes) for the settings to check before exposing the gateway
- **Reference**: [`auth.mcp`](../reference/configuration.md#authmcp) for every field that decides who may call the endpoint
