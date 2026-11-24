import ast
import pathlib

import pytest

import openapi_mcp_gateway


# The auth package's layers, and which other parts of ``auth`` each may import.
# The two sides know nothing of each other, and only the assembling layers join them.
# ``auth.assembly`` names types from both sides in the contracts it defines.
# The strategies build on those contracts, and ``auth.factory`` alone picks among them.
ALLOWED_AUTH_IMPORTS = {
    'auth.routes': set(),
    'auth.upstream': {'auth.upstream'},
    'auth.mcp': {'auth.mcp', 'auth.routes'},
    'auth.assembly': {'auth.routes', 'auth.upstream', 'auth.mcp'},
    'auth.settings_mapping': {'auth.mcp'},
    'auth.authorities': {'auth.assembly', 'auth.mcp', 'auth.settings_mapping'},
    'auth.flows': {
        'auth.assembly',
        'auth.authorities',
        'auth.flows',
        'auth.mcp',
        'auth.routes',
        'auth.settings_mapping',
        'auth.upstream',
    },
    'auth.factory': {'auth.assembly', 'auth.authorities', 'auth.flows', 'auth.upstream'},
}

# The package's own ``__init__``, which re-exports from every layer and which no layer may import.
_PACKAGE_INIT_LAYER = 'auth'

# The layers that speak a protocol, which take plain values and never read the config.
# Turning ``settings`` into those values belongs to the assembling layer, so a renamed key never reaches them.
PROTOCOL_LAYERS = ['auth.mcp', 'auth.routes', 'auth.upstream']

_PACKAGE_ROOT = pathlib.Path(openapi_mcp_gateway.__file__).parent


@pytest.mark.parametrize('layer', sorted(ALLOWED_AUTH_IMPORTS))
def test_a_layer_imports_only_what_lies_beneath_it(layer):
    """An import against the direction would couple the sides the auth package keeps apart."""
    offending = [
        f'{module} imports {target}'
        for module, targets in _auth_imports().items()
        if _layer_of(module) == layer
        for target in targets
        if _is_auth(target) and _layer_of(target) not in ALLOWED_AUTH_IMPORTS[layer]
    ]
    assert offending == []


def test_every_auth_layer_states_what_it_may_import():
    """A new module under ``auth`` names its layer's imports here, or it would escape the check above."""
    layers = {_layer_of(module) for module in _auth_imports()} - {_PACKAGE_INIT_LAYER}

    assert layers == set(ALLOWED_AUTH_IMPORTS)


@pytest.mark.parametrize('layer', PROTOCOL_LAYERS)
def test_a_protocol_layer_never_reads_the_config(layer):
    offending = [
        f'{module} imports settings'
        for module, targets in _auth_imports().items()
        if _layer_of(module) == layer and 'settings' in targets
    ]
    assert offending == []


def _auth_imports() -> dict[str, set[str]]:
    """Each module under ``auth``, as a dotted name below the package, with the modules it imports."""
    imports: dict[str, set[str]] = {}
    for path in (_PACKAGE_ROOT / 'auth').rglob('*.py'):
        parts = list(path.relative_to(_PACKAGE_ROOT).with_suffix('').parts)
        package = parts[:-1]
        module = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        targets = {
            target for node in ast.walk(ast.parse(path.read_text())) for target in _imported_modules(node, package)
        }
        imports[module] = targets
    return imports


def _imported_modules(node: ast.AST, package: list[str]) -> list[str]:
    """The modules ``node`` imports, as dotted names below the package, whether written relative or absolute.

    ``from . import x`` counts ``x`` as a module, since it may be one.
    """
    if isinstance(node, ast.Import):
        return [_below_package(alias.name) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    root = openapi_mcp_gateway.__name__
    if node.level:
        parts = package[: len(package) - (node.level - 1)] + (node.module.split('.') if node.module else [])
    elif node.module == root or (node.module or '').startswith(f'{root}.'):
        parts = (node.module or '').split('.')[1:]
    else:
        return []
    names = [alias.name for alias in node.names]
    # At the package root every imported name is a module of its own, such as ``settings``.
    if not parts:
        return names
    base = '.'.join(parts)
    if node.module is None:
        return [f'{base}.{name}' for name in names]
    return [base]


def _is_auth(module: str) -> bool:
    """Whether ``module``, a dotted name below the package, lies in ``auth``."""
    return module == 'auth' or module.startswith('auth.')


def _below_package(name: str) -> str:
    """``name`` without the package prefix, or unchanged when it names a module outside the package."""
    prefix = f'{openapi_mcp_gateway.__name__}.'
    return name.removeprefix(prefix)


def _layer_of(module: str) -> str:
    """The layer a module under ``auth`` belongs to, which is its subpackage or its own name."""
    parts = module.split('.')
    return '.'.join(parts[:2]) if len(parts) > 1 else module
