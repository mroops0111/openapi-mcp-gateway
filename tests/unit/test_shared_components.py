"""A component reached through several paths is advertised once, under ``$defs``, and referenced from each."""

import json
import typing

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer

from openapi_mcp_gateway import Gateway
from openapi_mcp_gateway.exposure import MetaToolGenerator, ToolGenerator, UpstreamBinding, build_input_schema
from openapi_mcp_gateway.exposure._shaping import shape_operation
from openapi_mcp_gateway.exposure.tool import _build_tool_signature
from openapi_mcp_gateway.openapi import McpIntegration, OperationInfo, ParamOverride, ToolOverride, parse_spec


_SOURCE_REFERENCE = {'$ref': '#/$defs/SourceReference'}


def _shared_component_spec() -> dict:
    """A body reaching ``SourceReference`` through three paths, plus a single-use and a recursive component."""
    source_reference = {'$ref': '#/components/schemas/SourceReference'}
    return {
        'openapi': '3.1.0',
        'info': {'title': 'Proposals', 'version': '1.0.0'},
        'servers': [{'url': 'https://api.example.com'}],
        'paths': {
            '/proposals': {
                'post': {
                    'operationId': 'createProposal',
                    'parameters': [
                        {
                            'name': 'status',
                            'in': 'query',
                            'description': 'Status to file the proposal under.',
                            'schema': {'$ref': '#/components/schemas/Status'},
                        }
                    ],
                    'requestBody': {
                        'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ProposalInput'}}}
                    },
                    'responses': {'201': {'description': 'ok'}},
                }
            }
        },
        'components': {
            'schemas': {
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
                        'children': {'type': 'array', 'items': {'$ref': '#/components/schemas/Node'}},
                    },
                },
                'Status': {'type': 'string', 'enum': ['open', 'closed']},
                'ProposalInput': {
                    'type': 'object',
                    'required': ['payload'],
                    'properties': {
                        'payload': source_reference,
                        'payloads': {'type': 'array', 'items': source_reference},
                        'patch': {'$ref': '#/components/schemas/Patch'},
                        'label': {'$ref': '#/components/schemas/Label'},
                        'tree': {'$ref': '#/components/schemas/Node'},
                    },
                },
            }
        },
    }


def _operation() -> OperationInfo:
    return parse_spec(_shared_component_spec()).operations[0]


def _register(generator_type: type[ToolGenerator] | type[MetaToolGenerator]) -> MCPServer:
    mcp = MCPServer('test')
    generator_type(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')).register([_operation()])
    return mcp


class TestAdvertisedSchema:
    """What ``build_input_schema`` advertises when a component is reached more than once."""

    def test_a_shape_reached_three_times_is_described_once(self):
        """Each path holds a reference, and the field descriptions appear once rather than once per path."""
        schema = build_input_schema(_operation())

        assert schema['properties']['payload'] == _SOURCE_REFERENCE
        assert schema['properties']['payloads']['items'] == _SOURCE_REFERENCE
        assert schema['properties']['patch']['properties']['sources']['items'] == _SOURCE_REFERENCE
        assert schema['$defs']['SourceReference']['properties']['line']['minimum'] == 1
        assert json.dumps(schema).count('First line covered') == 1

    def test_a_shape_reached_once_stays_inline(self):
        """Indirection buys nothing for a single use, so the schema reads as it did before ``$defs``."""
        schema = build_input_schema(_operation())

        assert schema['properties']['label'] == {
            'type': 'object',
            'description': 'A label.',
            'properties': {'text': {'type': 'string'}},
        }
        assert set(schema['$defs']) == {'SourceReference', 'Node'}

    def test_a_parameter_description_still_lands_beside_an_inlined_component(self):
        """A query parameter typed by a component keeps its own description, as when the component was expanded."""
        schema = build_input_schema(_operation())

        assert schema['properties']['status'] == {
            'type': 'string',
            'enum': ['open', 'closed'],
            'description': 'Status to file the proposal under.',
        }

    def test_a_recursive_component_is_expressed_rather_than_truncated(self):
        """``Node`` refers to itself through its own entry, so its shape is described at every depth."""
        schema = build_input_schema(_operation())

        assert schema['properties']['tree'] == {'$ref': '#/$defs/Node'}
        assert schema['$defs']['Node']['properties']['children']['items'] == {'$ref': '#/$defs/Node'}

    def test_a_hidden_parameter_takes_its_entries_with_it(self):
        """``$defs`` holds only what the visible surface reaches, so hiding a field cannot leave a stray entry."""
        operation = _operation()
        operation.x_mcp_integration = McpIntegration(
            tool=ToolOverride(params={'tree': ParamOverride(hidden=True)}, params_strategy='merge')
        )

        schema = build_input_schema(shape_operation(operation))

        assert 'tree' not in schema['properties']
        assert set(schema['$defs']) == {'SourceReference'}

    def test_a_tool_without_repetition_has_no_defs(self, petstore_json_path):
        """A spec with no shape reached twice advertises the fully inlined schema, unchanged from before."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(petstore_json_path))

        for tool in gateway.describe_servers()[0].tools:
            assert tool.input_schema is not None
            assert '$defs' not in tool.input_schema
            assert '$ref' not in json.dumps(tool.input_schema)

    def test_the_dry_run_summary_shows_the_advertised_form(self, tmp_path):
        """``describe()`` reports the same ``$defs`` form a client receives, not an expanded copy."""
        spec = tmp_path / 'spec.json'
        spec.write_text(json.dumps(_shared_component_spec()))
        gateway = Gateway()
        gateway.add_server(name='p', spec=str(spec))

        schema = gateway.describe()['servers'][0]['tools'][0]['input_schema']

        assert schema['properties']['payload'] == _SOURCE_REFERENCE
        assert set(schema['$defs']) == {'SourceReference', 'Node'}


class TestGeneratedModels:
    """The Python signature MCPServer parses arguments with."""

    def test_one_model_per_component_within_a_tool(self):
        """Every path to a component maps to the same generated class, namespaced by the operation."""
        signature, _ = _build_tool_signature(_operation())

        payload_type = signature.parameters['payload'].annotation
        payloads_type = typing.get_args(typing.get_args(signature.parameters['payloads'].annotation)[0])[0]
        assert payload_type is payloads_type
        assert payload_type.__name__ == 'CreateProposalSourceReference'


class TestOverTheWire:
    """A real client session sees, and is held to, the ``$defs`` form."""

    async def test_defs_survive_to_the_wire(self):
        """The SDK inlines only an output schema whose root is a bare ``$ref``, so nested ``$defs`` reach the client."""
        mcp = _register(ToolGenerator)

        async with Client(mcp) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'create_proposal')

        assert tool.input_schema == build_input_schema(_operation())
        assert tool.input_schema['properties']['payload'] == _SOURCE_REFERENCE
        assert set(tool.input_schema['$defs']) == {'SourceReference', 'Node'}

    async def test_nested_arguments_reach_the_upstream_intact(self, mock_upstream):
        """Arguments shaped by a referenced component, recursive ones included, are sent upstream as given."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['body'] = json.loads(request.content)
            captured['query'] = dict(request.url.params)
            return httpx.Response(201, json={'ok': True})

        mock_upstream(handler)
        mcp = _register(ToolGenerator)
        arguments = {
            'status': 'open',
            'payload': {'sourceId': 'docs', 'line': 3},
            'payloads': [{'sourceId': 'a'}, {'sourceId': 'b', 'line': 9}],
            'patch': {'sources': [{'sourceId': 'c', 'line': 1}]},
            'tree': {'label': 'root', 'children': [{'label': 'leaf', 'children': [{'label': 'deep'}]}]},
        }

        async with Client(mcp) as client:
            result = await client.call_tool('create_proposal', arguments)

        assert result.is_error is False
        assert captured['query'] == {'status': 'open'}
        assert captured['body'] == {key: value for key, value in arguments.items() if key != 'status'}

    async def test_a_bad_value_behind_a_reference_is_rejected(self, mock_upstream):
        """Validation follows the reference, so a constraint on the shared shape holds on every path."""
        mock_upstream(lambda request: pytest.fail('the upstream must not be called'))
        mcp = _register(ToolGenerator)

        async with Client(mcp) as client:
            result = await client.call_tool(
                'create_proposal',
                {'payload': {'sourceId': 'docs'}, 'payloads': [{'sourceId': 'a'}, {'sourceId': 'b', 'line': 0}]},
            )

        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content['errors'][0]['path'] == 'payloads/1/line'

    async def test_a_recursive_shape_is_enforced_at_depth(self, mock_upstream):
        """The truncated schema stopped describing ``children`` one level down, the recursive entry checks every level."""
        mock_upstream(lambda request: pytest.fail('the upstream must not be called'))
        mcp = _register(ToolGenerator)
        tree = {'label': 'root', 'children': [{'label': 'leaf', 'children': [{'children': []}]}]}

        async with Client(mcp) as client:
            result = await client.call_tool('create_proposal', {'payload': {'sourceId': 'docs'}, 'tree': tree})

        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content['errors'][0]['path'] == 'tree/children/0/children/0'

    async def test_dynamic_exposure_advertises_and_enforces_the_same_schema(self, mock_upstream):
        """``get_operation`` returns the ``$defs`` form, and ``call_operation`` validates against it."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['body'] = json.loads(request.content)
            return httpx.Response(201, json={'ok': True})

        mock_upstream(handler)
        mcp = _register(MetaToolGenerator)

        async with Client(mcp) as client:
            described = await client.call_tool('get_operation', {'name': 'create_proposal'})
            accepted = await client.call_tool(
                'call_operation',
                {'name': 'create_proposal', 'arguments': {'payload': {'sourceId': 'docs', 'line': 2}}},
            )
            rejected = await client.call_tool(
                'call_operation',
                {'name': 'create_proposal', 'arguments': {'payload': {'line': 2}}},
            )

        assert described.structured_content is not None
        assert described.structured_content['input_schema'] == build_input_schema(_operation())
        assert accepted.is_error is False
        assert captured['body'] == {'payload': {'sourceId': 'docs', 'line': 2}}
        assert rejected.is_error is True
