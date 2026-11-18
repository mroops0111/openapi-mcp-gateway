import ast
import pathlib

import pytest

import openapi_mcp_gateway


# The auth package's layers, and which other parts of ``auth`` each may import.
# The two sides know nothing of each other, and only the assembling layer joins them.
ALLOWED_AUTH_IMPORTS = {
    'auth.routes': set(),
    'auth.upstream': {'auth.upstream'},
    'auth.mcp': {'auth.mcp', 'auth.routes'},
    'auth.assembly': {'auth.routes', 'auth.upstream'},
}

_PACKAGE_ROOT = pathlib.Path(openapi_mcp_gateway.__file__).parent


@pytest.mark.parametrize('layer', sorted(ALLOWED_AUTH_IMPORTS))
def test_a_layer_imports_only_what_lies_beneath_it(layer):
    """An import against the direction would couple the sides the auth package keeps apart."""
    offending = [
        f'{module} imports {target}'
        for module, targets in _auth_imports().items()
        if _layer_of(module) == layer
        for target in targets
        if _layer_of(target) not in ALLOWED_AUTH_IMPORTS[layer]
    ]
    assert offending == []


def _auth_imports() -> dict[str, set[str]]:
    """Each module under ``auth``, as a dotted name below the package, with the ``auth`` modules it imports."""
    imports: dict[str, set[str]] = {}
    for path in (_PACKAGE_ROOT / 'auth').rglob('*.py'):
        parts = list(path.relative_to(_PACKAGE_ROOT).with_suffix('').parts)
        package = parts[:-1]
        module = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        targets = {
            target
            for node in ast.walk(ast.parse(path.read_text()))
            for target in _imported_modules(node, package)
            if target.startswith('auth.') or target == 'auth'
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
    if node.level:
        base = '.'.join(package[: len(package) - (node.level - 1)])
    else:
        base = _below_package(node.module or '')
        if node.module and base == node.module:
            return []
    if node.level and node.module:
        base = f'{base}.{node.module}'
    if node.module is None:
        return [f'{base}.{alias.name}' for alias in node.names]
    return [base]


def _below_package(name: str) -> str:
    """``name`` without the package prefix, or unchanged when it names a module outside the package."""
    prefix = f'{openapi_mcp_gateway.__name__}.'
    return name.removeprefix(prefix)


def _layer_of(module: str) -> str:
    """The layer a module under ``auth`` belongs to, which is its subpackage or its own name."""
    parts = module.split('.')
    return '.'.join(parts[:2]) if len(parts) > 1 else module
