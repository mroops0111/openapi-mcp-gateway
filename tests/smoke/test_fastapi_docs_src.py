"""Smoke against fastapi's official ``docs_src/`` tutorials.

The intent is to verify that real-world FastAPI patterns assemble cleanly through ``Gateway.from_fastapi``,
and produce at least one MCP tool.

We deliberately do not pin to specific tutorial filenames such as ``tutorial001_py310``,
since fastapi sometimes renames or removes those. Instead we glob the relevant category directory,
and pick the first importable module that exposes a ``FastAPI`` ``app``.

Tutorial code is not modified, so we use the public ``mark_tool`` helper to attach exposure metadata after the fact,
not the ``@mcp_tool`` decorator.
"""

import importlib
import pathlib
import pkgutil

import pytest
from fastapi import FastAPI
from mcp import Client

from openapi_mcp_gateway import Gateway, mark_tool


pytestmark = pytest.mark.smoke


_NO_AUTH_CATEGORIES = ('path_params', 'body', 'query_params_str_validations')


def _import_first_app(docs_src: pathlib.Path, category: str) -> tuple[str, FastAPI] | None:
    """Return the first ``(module_path, app)`` under ``docs_src/<category>/`` that imports cleanly.

    Modules that fail to import (missing optional deps, Python-version guards, etc.) are silently skipped,
    since we only need one working example per category for the smoke.
    Returns ``None`` if none import.
    """
    category_dir = docs_src / category
    if not category_dir.is_dir():
        return None
    for info in sorted(pkgutil.iter_modules([str(category_dir)]), key=lambda m: m.name):
        module_path = f'docs_src.{category}.{info.name}'
        try:
            module = importlib.import_module(module_path)
        except (ImportError, SyntaxError):
            # Expected version-skew (tutorial pinned to a Python version we are not on,
            # or an optional fastapi-extra dep is missing).
            # Other failures surface.
            continue
        app = getattr(module, 'app', None)
        if isinstance(app, FastAPI):
            return module_path, app
    return None


def _import_app_matching(docs_src: pathlib.Path, category: str, needle: str) -> tuple[str, FastAPI] | None:
    """Like ``_import_first_app`` but only returns modules whose source contains ``needle``.

    Used to pick out tutorials that exercise a specific security pattern (``OAuth2PasswordBearer``, ``HTTPBasic``,
    etc.) without naming the file.
    """
    category_dir = docs_src / category
    if not category_dir.is_dir():
        return None
    for info in sorted(pkgutil.iter_modules([str(category_dir)]), key=lambda m: m.name):
        py_file = category_dir / f'{info.name}.py'
        try:
            source = py_file.read_text(encoding='utf-8')
        except OSError:
            continue
        if needle not in source:
            continue
        module_path = f'docs_src.{category}.{info.name}'
        try:
            module = importlib.import_module(module_path)
        except (ImportError, SyntaxError):
            # Expected version-skew (tutorial pinned to a Python version we are not on,
            # or an optional fastapi-extra dep is missing).
            # Other failures surface.
            continue
        app = getattr(module, 'app', None)
        if isinstance(app, FastAPI):
            return module_path, app
    return None


def _mark_all_concrete_routes(app: FastAPI) -> int:
    """Mark every route whose endpoint is a regular function as an MCP tool.

    Returns the number of routes marked.
    We do not filter on path or method, since the goal is "whatever the tutorial defines, expose it".
    Lifespan handlers and other non-endpoint routes (no ``endpoint`` attribute) are skipped automatically.
    """
    marked = 0
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        if endpoint is None:
            continue
        mark_tool(endpoint)
        marked += 1
    return marked


@pytest.mark.parametrize('category', _NO_AUTH_CATEGORIES)
async def test_no_auth_tutorial_assembles(fastapi_docs_src: pathlib.Path, category: str):
    """A tutorial in each no-auth category assembles into a gateway with at least one MCP tool."""
    found = _import_first_app(fastapi_docs_src, category)
    assert found, f'no importable FastAPI tutorial under docs_src/{category}/'
    module_path, app = found

    marked = _mark_all_concrete_routes(app)
    assert marked, f'{module_path} has no endpoints to mark'

    gateway = Gateway.from_fastapi(app, name='smoke')
    bundle = gateway._servers[0]
    async with Client(bundle.mcp) as session:
        listed = await session.list_tools()
        assert listed.tools, f'{module_path} produced no MCP tools'


async def test_password_flow_tutorial_is_rejected(fastapi_docs_src: pathlib.Path):
    """Any security tutorial that uses ``OAuth2PasswordBearer`` is rejected with the expected error."""
    found = _import_app_matching(fastapi_docs_src, 'security', 'OAuth2PasswordBearer')
    if found is None:
        pytest.skip('no docs_src/security tutorial currently exercises OAuth2PasswordBearer')
    _, app = found

    _mark_all_concrete_routes(app)
    with pytest.raises(ValueError, match='unsupported OAuth2 flows'):
        Gateway.from_fastapi(app, name='smoke')


async def test_http_basic_tutorial_assembles(fastapi_docs_src: pathlib.Path):
    """A security tutorial that uses ``HTTPBasic`` assembles cleanly under NullAuth.

    The MCP transport used by this smoke does not carry HTTP headers, so we only verify that the tool is discoverable;
    header passthrough behaviour itself is covered by ``tests/integration/test_fastapi.py``.
    """
    found = _import_app_matching(fastapi_docs_src, 'security', 'HTTPBasic')
    if found is None:
        pytest.skip('no docs_src/security tutorial currently exercises HTTPBasic')
    _, app = found

    marked = _mark_all_concrete_routes(app)
    assert marked, 'HTTPBasic tutorial has no endpoints to mark'

    gateway = Gateway.from_fastapi(app, name='smoke')
    bundle = gateway._servers[0]
    async with Client(bundle.mcp) as session:
        listed = await session.list_tools()
        assert listed.tools
