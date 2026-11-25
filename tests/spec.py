import json
import pathlib
import typing

from tests.constants import API_URL


# The upstream scopes each petstore operation declares in its OpenAPI ``security``, keyed by operationId.
PETSTORE_UPSTREAM_SECURITY = {
    'listPets': [{'oauth2': ['read:pets']}],
    'getPetById': [{'oauth2': ['read:pets']}],
    'createPet': [{'oauth2': ['write:pets']}],
    'deletePet': [{'oauth2': ['read:pets', 'write:pets']}],
}


def component_ref(name: str) -> dict[str, str]:
    """A reference to the component schema ``name``, as a spec writes it."""
    return {'$ref': f'#/components/schemas/{name}'}


def defs_ref(name: str) -> dict[str, str]:
    """A reference to the ``$defs`` entry ``name``, as the gateway advertises it."""
    return {'$ref': f'#/$defs/{name}'}


def operation_spec(
    *,
    body: dict[str, typing.Any] | None = None,
    response: dict[str, typing.Any] | None = None,
    parameters: list[dict[str, typing.Any]] | None = None,
    schemas: dict[str, typing.Any] | None = None,
    operation_id: str = 'createThing',
    method: str = 'post',
    path: str = '/things',
    version: str = '3.1.0',
) -> dict[str, typing.Any]:
    """A spec with a single operation, served from ``API_URL``.

    ``body`` is the JSON request body schema, ``response`` the JSON schema of its ``200`` response,
    ``parameters`` the operation's own parameters, and ``schemas`` the component schemas any of them may refer to.
    """
    ok_response: dict[str, typing.Any] = {'description': 'ok'}
    if response is not None:
        ok_response['content'] = {'application/json': {'schema': response}}
    operation: dict[str, typing.Any] = {'operationId': operation_id, 'responses': {'200': ok_response}}
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


def write_spec(directory: pathlib.Path, spec: dict[str, typing.Any], name: str = 'spec.json') -> pathlib.Path:
    """Write ``spec`` as JSON to ``name`` under ``directory``, and return its path."""
    path = directory / name
    path.write_text(json.dumps(spec))
    return path


def write_secured_petstore(petstore_path: pathlib.Path, directory: pathlib.Path) -> pathlib.Path:
    """Write the petstore at ``petstore_path`` under ``directory`` with each operation's upstream scopes declared."""
    spec = json.loads(petstore_path.read_text())
    for path_item in spec['paths'].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and operation.get('operationId') in PETSTORE_UPSTREAM_SECURITY:
                operation['security'] = PETSTORE_UPSTREAM_SECURITY[operation['operationId']]
    return write_spec(directory, spec, 'secured-petstore.json')
