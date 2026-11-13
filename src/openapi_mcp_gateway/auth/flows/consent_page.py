import collections.abc
import dataclasses
import html
import typing
import urllib.parse

from starlette.responses import HTMLResponse


# The headers every gateway page carries.
# No page may be framed, which is what stops a clickjacked approval, and none loads anything beyond its own styles.
# ``form-action`` is left out, since Chrome applies it to the redirect a submitted form follows, here to the upstream.
PAGE_HEADERS = {
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
    'X-Frame-Options': 'DENY',
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
}

# The styles of every gateway page, in the colours of the project's logo, following the system's light or dark mode.
# They are inline so the content security policy can refuse everything else.
# The light action colour is a shade darker than the logo's, so white text on it keeps a 4.5:1 contrast.
_STYLES = """
:root { color-scheme: light dark; --background: #f6f2ea; --card: #ffffff; --line: #e5ddd0; --chip: #ece5d9;
  --ink: #1d1c1a; --muted: #6f6a62; --teal: #0c8599; --action: #0b7285; --on-action: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --background: #1d1c1a; --card: #262522; --line: #3b3832; --chip: #312f2b;
    --ink: #f2efe9; --muted: #a59f94; --teal: #3bc9db; --action: #3bc9db; --on-action: #10302f; }
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 40px 16px;
  background: var(--background); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; }
main { width: 100%; max-width: 440px; }
code, .mono { font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, monospace; }
.logo { display: block; width: 28px; height: 28px; margin: 0 0 18px 4px; }
.logo rect { fill: var(--ink); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; overflow: hidden; }
.bar { display: flex; gap: 7px; padding: 14px 18px 0; }
.bar i { width: 9px; height: 9px; border-radius: 50%; background: var(--line); }
.content { padding: 18px 28px 26px; }
.content > :last-child { margin-bottom: 0; }
h1 { font-size: 21px; line-height: 1.35; margin: 0 0 8px; font-weight: 650; letter-spacing: -0.01em; }
p { margin: 0 0 22px; color: var(--muted); }
.label { font-size: 13px; margin: 0 0 8px; }
.redirect { background: var(--chip); border-left: 3px solid var(--teal); border-radius: 0 10px 10px 0;
  padding: 12px 14px; font-size: 13.5px; overflow-wrap: anywhere; }
.hint { display: flex; gap: 8px; margin: 10px 0 22px; font-size: 13px; }
.hint svg { flex: none; margin-top: 3px; }
.hint circle, .hint path { stroke: var(--teal); }
ul { list-style: none; margin: 0 0 22px; padding: 0; display: grid; gap: 10px; }
li { display: flex; gap: 10px; align-items: baseline; overflow-wrap: anywhere; }
li::before { content: ""; flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--teal);
  transform: translateY(-2px); }
li span code { font-size: 12px; color: var(--muted); margin-left: 6px; }
details { font-size: 13px; color: var(--muted); padding-top: 14px; border-top: 1px dashed var(--line); }
summary { cursor: pointer; }
details p { margin: 8px 0 0; overflow-wrap: anywhere; }
.actions { display: flex; gap: 10px; margin-top: 22px; }
button { flex: 1; height: 44px; border-radius: 12px; border: 1px solid var(--line); background: transparent;
  color: var(--ink); font: 600 15px -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
  cursor: pointer; }
button.approve { background: var(--action); border-color: var(--action); color: var(--on-action); }
button:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
"""

# The project's logo, an arch in a rounded square, drawn inline since the page loads nothing.
_LOGO = (
    '<svg class="logo" viewBox="0 0 100 100" aria-hidden="true"><rect width="100" height="100" rx="24"/>'
    '<path d="M29 74 L29 50 A21 21 0 0 1 71 50 L71 74" fill="none" stroke="#0c8599" stroke-width="11" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

# The mark beside the warning to check who started the sign-in.
_HINT_ICON = (
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">'
    '<circle cx="8" cy="8" r="7" fill="none" stroke-width="1.6"/>'
    '<path d="M8 4.5v4.2M8 11.2v.3" stroke-width="1.8" stroke-linecap="round"/></svg>'
)


class DescribedScope(typing.NamedTuple):
    """A scope as the consent page lists it, led by what it allows when anything says so."""

    name: str
    description: str | None


@dataclasses.dataclass(frozen=True)
class ConsentPrompt:
    """What the consent page shows, so a user can tell whether the client asking is the one they are using.

    ``client_name`` is whatever the client registered itself as, so the page shows ``redirect_uri`` first,
    the address the authorization code is sent to, which a client cannot fake.
    ``mcp_scopes`` is ``None`` on a server without ``auth.operation_scopes``, where a token may use every tool.
    """

    server_name: str
    upstream_host: str
    client_id: str
    client_name: str | None
    redirect_uri: str
    upstream_scopes: collections.abc.Sequence[DescribedScope]
    mcp_scopes: collections.abc.Sequence[DescribedScope] | None
    csrf_token: str


def consent_page(prompt: ConsentPrompt) -> HTMLResponse:
    """The consent page for ``prompt``, posting the user's decision back to the address it was served from."""
    client = html.escape(prompt.client_name or prompt.client_id)
    server = html.escape(prompt.server_name)
    upstream_host = html.escape(prompt.upstream_host)
    upstream_scopes = (
        f'<p class="label">{upstream_host} will be asked for</p>{_scope_list(prompt.upstream_scopes, "")}'
        if prompt.upstream_scopes
        else ''
    )
    content = f"""
<h1>Allow {client} to use {server}?</h1>
<p>You sign in at <b>{upstream_host}</b> next, then {client} can use {server} on your behalf.</p>
<p class="label">Access is sent to</p>
<div class="redirect mono">{_redirect_label(prompt.redirect_uri)}</div>
<p class="hint">{_HINT_ICON}<span>Approve only if you just started this sign-in from {client}.</span></p>
<p class="label">{client} will be able to</p>
{_mcp_scope_list(prompt.mcp_scopes, server)}
{upstream_scopes}
<details><summary>Client details</summary><p>Registered as <code>{html.escape(prompt.client_id)}</code></p></details>
<form method="post">
  <input type="hidden" name="csrf_token" value="{html.escape(prompt.csrf_token)}">
  <div class="actions">
    <button type="submit" name="decision" value="deny">Deny</button>
    <button type="submit" name="decision" value="approve" class="approve">Approve</button>
  </div>
</form>"""
    return _page('Approve sign-in', content)


def error_page(message: str, status_code: int) -> HTMLResponse:
    """A page telling the user why a sign-in cannot go on, and that it has to start again from the client."""
    content = f"""
<h1>Sign-in cannot continue</h1>
<p>{html.escape(message)}</p>
<p>Start the sign-in again from your MCP client.</p>"""
    return _page('Sign-in cannot continue', content, status_code)


def _page(title: str, content: str, status_code: int = 200) -> HTMLResponse:
    """A whole gateway page, with ``content``, which must already be escaped, in the card under the logo."""
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{html.escape(title)}</title><style>{_STYLES}</style></head>'
        f'<body><main>{_LOGO}<div class="card"><div class="bar"><i></i><i></i><i></i></div>'
        f'<div class="content">{content}</div></div></main></body></html>'
    )
    return HTMLResponse(document, status_code=status_code, headers=PAGE_HEADERS)


def _redirect_label(redirect_uri: str) -> str:
    """``redirect_uri`` with its host in bold, the part a user checks against the client they are using."""
    origin = urllib.parse.urlsplit(redirect_uri)
    scheme = f'{origin.scheme}://'
    rest = redirect_uri.removeprefix(f'{scheme}{origin.netloc}')
    return f'{html.escape(scheme)}<b>{html.escape(origin.netloc)}</b>{html.escape(rest)}'


def _mcp_scope_list(scopes: collections.abc.Sequence[DescribedScope] | None, server: str) -> str:
    """What the client may do on ``server``, which is every tool on a server that defines no MCP scopes."""
    if scopes is None:
        return f'<ul><li>Use every tool on {server}</li></ul>'
    return _scope_list(scopes, f'No tools on {server}')


def _scope_list(scopes: collections.abc.Sequence[DescribedScope], empty: str) -> str:
    """``scopes`` as a list, each led by its description with its name beside it, or by its name alone.

    ``empty`` is the one item an empty list shows, already escaped.
    """
    items = (
        f'<li><span>{html.escape(scope.description)}<code>{html.escape(scope.name)}</code></span></li>'
        if scope.description
        else f'<li><code>{html.escape(scope.name)}</code></li>'
        for scope in scopes
    )
    return f'<ul>{"".join(items) or f"<li>{empty}</li>"}</ul>'
