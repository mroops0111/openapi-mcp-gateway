import json
import pathlib
import typing

from tests.constants import API_URL


# The upstream scopes each shop operation declares in its OpenAPI ``security``, keyed by operationId.
SHOP_UPSTREAM_SECURITY = {
    'listProducts': [{'oauth2': ['read:products']}],
    'getProductById': [{'oauth2': ['read:products']}],
    'createProduct': [{'oauth2': ['write:products']}],
    'deleteProduct': [{'oauth2': ['read:products', 'write:products']}],
}

# A Trello-like API, whose every call needs two query keys together.
TRELLO_SCHEMES = {
    'APIKey': {'type': 'apiKey', 'in': 'query', 'name': 'key'},
    'APIToken': {'type': 'apiKey', 'in': 'query', 'name': 'token'},
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


def combined_spec(*specs: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """The first of ``specs`` with the operations of every other one added to its paths."""
    combined = {**specs[0], 'paths': {}}
    for spec in specs:
        for path, path_item in spec['paths'].items():
            combined['paths'].setdefault(path, {}).update(path_item)
    return combined


def write_spec(directory: pathlib.Path, spec: dict[str, typing.Any], name: str = 'spec.json') -> pathlib.Path:
    """Write ``spec`` as JSON to ``name`` under ``directory``, and return its path."""
    path = directory / name
    path.write_text(json.dumps(spec))
    return path


def write_secured_shop(shop_path: pathlib.Path, directory: pathlib.Path) -> pathlib.Path:
    """Write the shop at ``shop_path`` under ``directory`` with each operation's upstream scopes declared."""
    spec = json.loads(shop_path.read_text())
    for path_item in spec['paths'].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and operation.get('operationId') in SHOP_UPSTREAM_SECURITY:
                operation['security'] = SHOP_UPSTREAM_SECURITY[operation['operationId']]
    return write_spec(directory, spec, 'secured-shop.json')


def write_board_spec(
    tmp_path: pathlib.Path,
    *,
    schemes: dict[str, typing.Any] | None = None,
    swagger_definitions: dict[str, typing.Any] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    operations: dict[str, list[dict[str, list[str]]]] | None = None,
) -> pathlib.Path:
    """A board API served from ``API_URL``, with ``security`` at the root or each of ``operations`` its own.

    ``getBoard`` is a GET and ``updateBoard`` a PUT on the same path.
    """
    securities = operations or {'getBoard': None}
    path_item = {
        method: {
            'operationId': operation_id,
            'parameters': [{'name': 'board_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'ok'}},
            **({'security': requirement} if requirement is not None else {}),
        }
        for operation_id, requirement in securities.items()
        for method in ['get' if operation_id == 'getBoard' else 'put']
    }
    document: dict[str, typing.Any] = {
        'openapi': '3.1.0',
        'info': {'title': 'Boards', 'version': '1'},
        'servers': [{'url': API_URL}],
        'paths': {'/boards/{board_id}': path_item},
    }
    if schemes is not None:
        document['components'] = {'securitySchemes': schemes}
    if swagger_definitions is not None:
        document['securityDefinitions'] = swagger_definitions
    if security is not None:
        document['security'] = security
    path = tmp_path / 'boards.json'
    path.write_text(json.dumps(document))
    return path


def nested_body_spec() -> dict:
    """One operation whose body nests an object, an array of objects, and an enum with a default."""
    return operation_spec(
        operation_id='createOrder',
        path='/orders',
        version='3.0.0',
        body={
            'type': 'object',
            'required': ['customer'],
            'properties': {
                'customer': {'type': 'object', 'properties': {'id': {'type': 'integer'}}},
                'items': {
                    'type': 'array',
                    'items': {'type': 'object', 'properties': {'qty': {'type': 'integer', 'minimum': 1}}},
                },
                'priority': {'type': 'string', 'enum': ['low', 'normal', 'rush'], 'default': 'normal'},
            },
        },
    )


def shared_component_spec() -> dict:
    """A body reaching ``SourceReference`` through three paths, plus a single-use and a recursive component."""
    source_reference = component_ref('SourceReference')
    return operation_spec(
        operation_id='createProposal',
        path='/proposals',
        parameters=[
            {
                'name': 'status',
                'in': 'query',
                'description': 'Status to file the proposal under.',
                'schema': component_ref('Status'),
            }
        ],
        body=component_ref('ProposalInput'),
        schemas={
            'SourceReference': {
                'type': 'object',
                'required': ['sourceId'],
                'properties': {
                    'sourceId': {'type': 'string', 'description': 'Id of a source the workspace has registered.'},
                    'line': {'type': 'integer', 'minimum': 1, 'description': 'First line covered, counted from 1.'},
                },
            },
            'Patch': {'type': 'object', 'properties': {'sources': {'type': 'array', 'items': source_reference}}},
            'Label': {'type': 'object', 'description': 'A label.', 'properties': {'text': {'type': 'string'}}},
            'Node': {
                'type': 'object',
                'required': ['label'],
                'properties': {
                    'label': {'type': 'string'},
                    'children': {'type': 'array', 'items': component_ref('Node')},
                },
            },
            'Status': {'type': 'string', 'enum': ['open', 'closed']},
            'ProposalInput': {
                'type': 'object',
                'required': ['payload'],
                'properties': {
                    'payload': source_reference,
                    'payloads': {'type': 'array', 'items': source_reference},
                    'patch': component_ref('Patch'),
                    'label': component_ref('Label'),
                    'tree': component_ref('Node'),
                },
            },
        },
    )
