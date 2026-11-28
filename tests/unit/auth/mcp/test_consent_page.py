import dataclasses

import pytest

from openapi_mcp_gateway.auth.mcp.consent_page import McpConsentPrompt, McpDescribedScope, render_consent_page


# A prompt for a client that registered itself under a name meant to break out of the page.
HOSTILE_PROMPT = McpConsentPrompt(
    server_name='shop',
    next_sign_in_host='auth.example.com',
    client_id='client-1',
    client_name='<script>alert(1)</script>',
    redirect_uri='https://app.example.com/cb?next="><img src=x>',
    next_sign_in_scopes=[McpDescribedScope('read', None)],
    mcp_scopes=[McpDescribedScope('products:read', '<b>Read</b> products')],
    opens_unscoped_tools=False,
    csrf_token='token',
)


class TestConsentPage:
    """Everything a client supplies reaches the page escaped."""

    def test_a_hostile_client_name_is_escaped(self):
        page = _page_text(HOSTILE_PROMPT)
        assert '<script>' not in page
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page

    def test_a_hostile_redirect_uri_is_escaped(self):
        page = _page_text(HOSTILE_PROMPT)
        assert '<img' not in page

    def test_a_client_without_a_name_is_shown_by_its_id(self):
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, client_name=None))
        assert 'Allow client-1 to use shop?' in page

    def test_a_document_client_is_named_with_its_verified_host(self):
        prompt = dataclasses.replace(HOSTILE_PROMPT, client_name='Claude Code', client_host='claude.ai')
        assert 'Allow Claude Code (claude.ai) to use shop?' in _page_text(prompt)

    def test_a_hostile_client_host_is_escaped(self):
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, client_host='<i>host</i>'))
        assert '(&lt;i&gt;host&lt;/i&gt;)' in page

    @pytest.mark.parametrize(
        ('redirect_uri', 'warned'), [('http://127.0.0.1:3000/callback', True), ('https://app.example.com/cb', False)]
    )
    def test_a_loopback_redirect_warns_that_any_local_program_could_ask(self, redirect_uri, warned):
        """Any program listening on this computer could have started the sign-in under the client's name."""
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, client_name='agent', redirect_uri=redirect_uri))
        assert ('a program on this computer' in page) is warned

    def test_a_hostile_scope_description_is_escaped(self):
        assert '&lt;b&gt;Read&lt;/b&gt; products<code class="aside">products:read</code>' in _page_text(HOSTILE_PROMPT)

    def test_a_scope_a_step_up_adds_is_marked_new(self):
        prompt = dataclasses.replace(
            HOSTILE_PROMPT, mcp_scopes=[McpDescribedScope('products:write', 'Write products', new=True)]
        )
        assert 'Write products<code class="aside">products:write</code><span class="new">New</span>' in _page_text(
            prompt
        )

    def test_a_scope_without_a_description_is_shown_by_its_name(self):
        assert '<li><span><code>read</code></span></li>' in _page_text(HOSTILE_PROMPT)

    @pytest.mark.parametrize('mcp_scopes', [[], HOSTILE_PROMPT.mcp_scopes])
    def test_tools_open_to_every_client_are_named(self, mcp_scopes):
        """A list of scopes alone would understate what a server that opens unscoped tools grants."""
        prompt = dataclasses.replace(HOSTILE_PROMPT, mcp_scopes=mcp_scopes, opens_unscoped_tools=True)
        page = _page_text(prompt)
        assert 'Use the tools on shop open to every client' in page
        assert 'No tools' not in page

    def test_a_client_with_nothing_open_to_it_is_told_so(self):
        assert 'No tools on shop' in _page_text(dataclasses.replace(HOSTILE_PROMPT, mcp_scopes=[]))

    def test_no_next_sign_in_scopes_leaves_their_list_out(self):
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, next_sign_in_scopes=[]))
        assert 'will be asked for' not in page

    def test_a_sign_in_that_ends_at_approval_names_no_next_host(self):
        """With no sign-in after consent, the page does not promise one."""
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, next_sign_in_host=None, next_sign_in_scopes=[]))
        assert 'You sign in at' not in page
        assert 'Then client-1 can use shop on your behalf.' in _page_text(
            dataclasses.replace(HOSTILE_PROMPT, client_name=None, next_sign_in_host=None, next_sign_in_scopes=[])
        )


def _page_text(prompt: McpConsentPrompt) -> str:
    """The HTML the consent page for ``prompt`` serves."""
    return bytes(render_consent_page(prompt).body).decode()
