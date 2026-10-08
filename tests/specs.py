import typing

from tests.constants import API_URL


def component_ref(name: str) -> dict[str, str]:
    """A reference to the component schema ``name``, as a spec writes it."""
    return {'$ref': f'#/components/schemas/{name}'}


def defs_ref(name: str) -> dict[str, str]:
    """A reference to the ``$defs`` entry ``name``, as the gateway advertises it."""
    return {'$ref': f'#/$defs/{name}'}


def operation_spec(
    *,
    body: dict[str, typing.Any] | None = None,
    parameters: list[dict[str, typing.Any]] | None = None,
    schemas: dict[str, typing.Any] | None = None,
    operation_id: str = 'createThing',
    method: str = 'post',
    path: str = '/things',
    version: str = '3.1.0',
) -> dict[str, typing.Any]:
    """A spec with a single operation, served from ``API_URL``.

    ``body`` is the JSON request body schema, ``parameters`` the operation's own parameters,
    and ``schemas`` the component schemas either may refer to.
    """
    operation: dict[str, typing.Any] = {'operationId': operation_id, 'responses': {'200': {'description': 'ok'}}}
    if parameters is not None:
        operation['parameters'] = parameters
    if body is not None:
        operation['requestBody'] = {'content': {'application/json': {'schema': body}}}
    spec: dict[str, typing.Any] = {
        'openapi': version,
        'info': {'title': 'Test API', 'version': '1.0.0'},
        'servers': [{'url': API_URL}],
        'paths': {path: {method: operation}},
    }
    if schemas is not None:
        spec['components'] = {'schemas': schemas}
    return spec
