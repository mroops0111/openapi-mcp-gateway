import collections
import inspect
import json
import re
import types
import typing

import httpx
import jsonschema
import pydantic
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import TextContent

from openapi_mcp_gateway import Gateway
from openapi_mcp_gateway.exposure import (
    MetaToolGenerator,
    ToolGenerator,
    UpstreamConnection,
    build_input_schema,
    derive_description,
    derive_name,
)
from openapi_mcp_gateway.exposure._shaping import shape_operation
from openapi_mcp_gateway.exposure._shared import (
    _sanitize_name,
    _schema_to_python_type,
    build_input_schema,
)
from openapi_mcp_gateway.exposure.tool import _build_tool_signature
from openapi_mcp_gateway.openapi import McpIntegration, OperationInfo, ParamOverride, ToolOverride, parse_spec
from tests.constants import API_URL
from tests.operations import expose_resource
from tests.specs import component_ref, defs_ref, operation_spec


# The ``$defs`` key grammar Anthropic's API accepts in a tool input schema,
# the strictest a client may hand the advertised schema to.
_PROVIDER_DEFS_KEY = re.compile(r'^[a-zA-Z0-9_.-]{1,64}$')


# Arguments filling every property of ``_colliding_names_spec``, each object with its own distinct field.
_COLLIDING_NAMES_ARGUMENTS = {
    'owner': {'name': 'n'},
    'keeper': {'title': 't'},
    'pet': {'tag': {'label': 'l'}},
    'badge': {'code': 'c'},
    'tags': [{'label': 'l'}],
    'marker': {'slot': 's'},
    'choice': {'right': 'r'},
    'pick': {'pick': 'p'},
    'a_b': {'c': {'via_ab': 'x'}},
    'a': {'b_c': {'via_bc': 'y'}},
    'spellings': {'tag_name': {'snake': 's'}, 'tagName': {'camel': 'c'}},
    'variants': {
        'snake': {'snake': '1'},
        'camel': {'camel': '2'},
        'pascal': {'pascal': '3'},
        'kebab': {'kebab': '4'},
        'dotted': {'dotted': '5'},
    },
}


class TestSanitizeName:
    """Conversion of arbitrary OpenAPI names into valid Python identifiers."""

    def test_slash_to_underscore(self):
        """Slashes become underscores."""
        assert _sanitize_name('meta/root') == 'meta_root'

    def test_dash_to_underscore(self):
        """Dashes become underscores."""
        assert _sanitize_name('enterprise-team') == 'enterprise_team'

    def test_dot_to_underscore(self):
        """Dots become underscores."""
        assert _sanitize_name('foo.bar') == 'foo_bar'

    def test_leading_digit_prefixed(self):
        """A leading digit gets prefixed with ``_`` to form a valid identifier."""
        assert _sanitize_name('1st_param') == '_1st_param'

    def test_already_valid_unchanged(self):
        """A name that is already a valid identifier is unchanged."""
        assert _sanitize_name('valid_name') == 'valid_name'

    def test_python_keyword_suffixed(self):
        """Python keywords get a trailing underscore to avoid syntax errors."""
        assert _sanitize_name('async') == 'async_'
        assert _sanitize_name('class') == 'class_'
        assert _sanitize_name('from') == 'from_'


class TestSchemaToPythonType:
    """Conversion of JSON Schema fragments into Python type annotations."""

    def test_missing_type_falls_back_to_any(self):
        """A fragment with no ``type`` resolves to ``Any``, not ``str``."""
        assert _schema_to_python_type({'format': 'date-time'}) is typing.Any
        assert _schema_to_python_type({}) is typing.Any

    def test_anyof_yields_union(self):
        """``anyOf`` variants are resolved into a Union."""
        result = _schema_to_python_type({'anyOf': [{'type': 'string'}, {'type': 'integer'}]})
        assert isinstance(result, types.UnionType)
        assert set(typing.get_args(result)) == {str, int}

    def test_array_of_oneof_objects_is_not_list_of_strings(self):
        """An array whose items are a ``oneOf`` of objects does not map to ``list[str]``.

        Regression: the ``oneOf`` fragment omits a top-level ``type``,
        so the old ``'string'`` default collapsed the items to ``str``.
        """
        schema = {
            'type': 'array',
            'items': {
                'oneOf': [
                    {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['a']}}},
                    {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['b']}}},
                ],
            },
        }
        result = _schema_to_python_type(schema)
        assert typing.get_origin(result) is list
        assert typing.get_args(result)[0] is not str

    def test_enum_yields_literal(self):
        """``enum`` collapses to ``Literal`` so the allowed values appear inline instead of hiding behind ``str``."""
        result = _schema_to_python_type({'type': 'string', 'enum': ['left', 'center', 'right']})
        assert typing.get_origin(result) is typing.Literal
        assert set(typing.get_args(result)) == {'left', 'center', 'right'}

    def test_nullable_list_type_becomes_optional(self):
        """A 2020-12 array ``type`` like ``["integer", "null"]`` maps to ``int | None``."""
        result = _schema_to_python_type({'type': ['integer', 'null']})
        assert isinstance(result, types.UnionType)
        assert set(typing.get_args(result)) == {int, type(None)}

    def test_object_with_properties_yields_typed_model(self):
        """An object with ``properties`` becomes a typed pydantic model rather than ``dict[str, Any]``.

        Nested field descriptions and required-ness then survive into the generated JSON Schema.
        """
        import pydantic

        schema = {
            'type': 'object',
            'required': ['street', 'city'],
            'properties': {
                'street': {'type': 'string', 'description': 'Street address.'},
                'city': {'type': 'string', 'description': 'City name.'},
                'country': {'type': 'string', 'description': 'ISO 3166-1 alpha-2 country code.'},
            },
        }
        model_type = _schema_to_python_type(schema, name_hint='Address')
        assert issubclass(model_type, pydantic.BaseModel)
        json_schema = model_type.model_json_schema()
        assert set(json_schema['required']) == {'street', 'city'}
        assert json_schema['properties']['street']['description'] == 'Street address.'
        assert json_schema['properties']['country']['description'] == 'ISO 3166-1 alpha-2 country code.'

    def test_object_without_properties_falls_back_to_dict(self):
        """An untyped ``object`` (no ``properties``) keeps the ``dict[str, Any]`` shape for free-form maps."""
        result = _schema_to_python_type({'type': 'object'})
        assert typing.get_origin(result) is dict
        assert typing.get_args(result) == (str, typing.Any)

    def test_nested_object_inside_array_preserves_descriptions(self):
        """``array<object>`` recurses into each property and keeps the nested descriptions.

        This is the common shape for list-valued request body parameters.
        """
        import pydantic

        schema = {
            'type': 'array',
            'items': {
                'type': 'object',
                'required': ['id'],
                'properties': {
                    'id': {'type': 'integer', 'description': 'Identifier.'},
                    'name': {'type': 'string', 'description': 'Display name.'},
                },
            },
        }
        list_type = _schema_to_python_type(schema, name_hint='LineItems')
        item_type = typing.get_args(list_type)[0]
        assert issubclass(item_type, pydantic.BaseModel)
        json_schema = item_type.model_json_schema()
        assert json_schema['required'] == ['id']
        assert json_schema['properties']['name']['description'] == 'Display name.'


class TestExposedToolDetail:
    """A caller deciding what to expose reads the same contract the model is given."""

    def test_the_description_is_the_one_the_model_receives(self, petstore_json_path):
        """Reporting anything else would let the preview and the served tool disagree."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(petstore_json_path))
        tool = next(t for t in gateway.describe_servers()[0].tools if t.name == 'get_pet_by_id')

        assert tool.description
        assert tool.description != tool.name

    def test_the_schema_is_the_advertised_one_rather_than_a_copy(self, petstore_json_path):
        """A second representation is a second thing to keep in step, so there is only one."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(petstore_json_path))
        bundle = gateway.describe_servers()[0]
        tool = next(t for t in bundle.tools if t.name == 'get_pet_by_id')
        operation = next(o for o in bundle.spec.operations if o.operation_id == 'getPetById')

        assert tool.input_schema == build_input_schema(operation)

    def test_a_nested_body_keeps_its_shape(self, tmp_path):
        """Flattening body properties into a list loses exactly what a reviewer needs.

        A summary that reports ``priority: string`` cannot answer what the model may send.
        The answer lives below the top level, in an enum, a default, a pattern and a numeric bound.
        """
        spec = tmp_path / 'spec.json'
        spec.write_text(json.dumps(_nested_body_spec()))
        gateway = Gateway()
        gateway.add_server(name='o', spec=str(spec))
        schema = gateway.describe_servers()[0].tools[0].input_schema

        assert schema is not None
        assert schema['properties']['priority']['enum'] == ['low', 'normal', 'rush']
        assert schema['properties']['priority']['default'] == 'normal'
        assert schema['properties']['customer']['properties']['id']['type'] == 'integer'
        assert schema['properties']['items']['items']['properties']['qty']['minimum'] == 1
        assert schema['required'] == ['customer']


class TestAdvertisedSchema:
    """What ``build_input_schema`` advertises when a component is reached more than once."""

    def test_a_shape_reached_three_times_is_described_once(self):
        """Each path holds a reference, and the field descriptions appear once rather than once per path."""
        schema = _advertised(_operation())

        assert schema['properties']['payload'] == defs_ref('SourceReference')
        assert schema['properties']['payloads']['items'] == defs_ref('SourceReference')
        assert schema['properties']['patch']['properties']['sources']['items'] == defs_ref('SourceReference')
        assert schema['$defs']['SourceReference']['properties']['line']['minimum'] == 1
        assert json.dumps(schema).count('First line covered') == 1

    def test_a_shape_reached_once_stays_inline(self):
        """Indirection buys nothing for a single use, so the schema reads as it did before ``$defs``."""
        schema = _advertised(_operation())

        assert schema['properties']['label'] == {
            'type': 'object',
            'description': 'A label.',
            'properties': {'text': {'type': 'string'}},
        }
        assert set(schema['$defs']) == {'SourceReference', 'Node'}

    def test_a_description_beside_a_reference_describes_that_use(self):
        """Two uses of one shared component each keep their own description.

        An inlined use overrides the component's description with its own.
        """
        raw = operation_spec(
            body={
                'type': 'object',
                'properties': {
                    'owner': {**component_ref('Person'), 'description': 'Who owns it.'},
                    'approver': {**component_ref('Person'), 'description': 'Who signs it off.'},
                    'label': {**component_ref('Label'), 'description': 'Shown on the card.'},
                },
            },
            schemas={
                'Person': _object('name'),
                'Label': {'type': 'object', 'description': 'A label.', 'properties': {'text': {'type': 'string'}}},
            },
        )

        schema = _advertised(_operation(raw))

        assert schema['properties']['owner'] == {**defs_ref('Person'), 'description': 'Who owns it.'}
        assert schema['properties']['approver'] == {**defs_ref('Person'), 'description': 'Who signs it off.'}
        assert schema['properties']['label'] == {
            'type': 'object',
            'description': 'Shown on the card.',
            'properties': {'text': {'type': 'string'}},
        }

    def test_a_parameter_description_still_lands_beside_an_inlined_component(self):
        """A query parameter typed by a component keeps its own description, as when the component was expanded."""
        schema = _advertised(_operation())

        assert schema['properties']['status'] == {
            'type': 'string',
            'enum': ['open', 'closed'],
            'description': 'Status to file the proposal under.',
        }

    def test_a_recursive_component_is_expressed_rather_than_truncated(self):
        """``Node`` refers to itself through its own entry, so its shape is described at every depth."""
        schema = _advertised(_operation())

        assert schema['properties']['tree'] == defs_ref('Node')
        assert schema['$defs']['Node']['properties']['children']['items'] == defs_ref('Node')

    def test_a_hidden_parameter_takes_its_entries_with_it(self):
        """``$defs`` holds only what the visible surface reaches, so hiding a field cannot leave a stray entry."""
        operation = _operation()
        operation.x_mcp_integration = McpIntegration(
            tool=ToolOverride(params={'tree': ParamOverride(hidden=True)}, params_strategy='merge')
        )

        schema = _advertised(shape_operation(operation))

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

        assert schema['properties']['payload'] == defs_ref('SourceReference')
        assert set(schema['$defs']) == {'SourceReference', 'Node'}

    def test_defs_keys_fit_what_providers_accept(self):
        """Component names past 64 characters still advertise keys a strict client passes on, one per component.

        A key outside the grammar gets the whole tool list rejected, not just the one tool.
        """
        schema = _advertised(_operation(_long_names_spec()))

        assert len(schema['$defs']) == 2
        assert all(_PROVIDER_DEFS_KEY.match(key) for key in schema['$defs'])
        assert _accepts(schema, {'first': {'name': 'n'}, 'seconds': [{'pattern': 'p'}]})
        assert not _accepts(schema, {'firsts': [{'pattern': 'p'}]})
        assert not _accepts(schema, {'second': {'name': 'n'}})


class TestAllOfOverAReference:
    """An ``allOf`` extending a schema whose properties are typed by components adds to them rather than replacing.

    ``Person`` is checked both inlined at its one use and shared under ``$defs``.
    """

    @pytest.mark.parametrize('person_shared', [False, True], ids=['inlined', 'shared'])
    def test_an_extension_keeps_the_referenced_fields(self, person_shared):
        """``owner`` is a ``Person`` plus the extension's ``extra``, requiring both ``name`` and ``id``."""
        schema = _advertised(_operation(_extended_body_spec(person_shared=person_shared)))
        approver = {'name': 'r', 'role': 'admin'}

        assert _accepts(schema, {'owner': {'id': 1, 'name': 'n', 'extra': 'e'}, 'approver': approver})
        assert not _accepts(schema, {'owner': {'id': 1}, 'approver': approver})
        assert not _accepts(schema, {'owner': {'name': 'n'}, 'approver': approver})
        assert 'Person name.' in json.dumps(schema)
        assert 'Added by the extension.' in json.dumps(schema)

    def test_two_components_typing_one_property_both_hold(self):
        """``approver`` is a ``Reviewer`` from ``Base`` and an ``Admin`` from the extension.

        Both components' fields count.
        """
        schema = _advertised(_operation(_extended_body_spec(person_shared=False)))
        owner = {'id': 1, 'name': 'n'}

        assert _accepts(schema, {'owner': owner, 'approver': {'name': 'r', 'role': 'admin'}})
        assert not _accepts(schema, {'owner': owner, 'approver': {'role': 'admin'}})
        assert not _accepts(schema, {'owner': owner, 'approver': {'name': 'r'}})
        assert 'Reviewer name.' in json.dumps(schema)
        assert 'Admin role.' in json.dumps(schema)

    @pytest.mark.parametrize('person_shared', [False, True], ids=['inlined', 'shared'])
    async def test_every_field_reaches_the_upstream(self, upstream_requests, person_shared):
        """The parsed arguments keep the fields of every schema involved, not only those of the last component."""
        operation = _operation(_extended_body_spec(person_shared=person_shared))
        arguments = {'owner': {'id': 1, 'name': 'n', 'extra': 'e'}, 'approver': {'name': 'r', 'role': 'admin'}}

        async with Client(_register(ToolGenerator, operation)) as client:
            result = await client.call_tool('create_ticket', arguments)

        assert result.is_error is False
        assert _upstream_body(upstream_requests) == arguments


class TestGeneratedModels:
    """The Python signature MCPServer parses arguments with."""

    def test_one_model_per_component_within_a_tool(self):
        """Every path to a component maps to the same generated class, namespaced by the operation."""
        signature, _ = _build_tool_signature(_operation())

        payload_type = signature.parameters['payload'].annotation
        payloads_type = typing.get_args(typing.get_args(signature.parameters['payloads'].annotation)[0])[0]
        assert payload_type is payloads_type
        assert payload_type.__name__ == 'CreateProposalSourceReference'

    def test_a_recursive_component_is_its_own_field_type(self):
        """``Node.children`` holds ``Node`` itself, so every level is parsed the same way rather than as ``Any``."""
        signature, _ = _build_tool_signature(_operation())

        node = _model(signature.parameters['tree'].annotation)

        assert _model(node.model_fields['children'].annotation) is node

    @pytest.mark.parametrize('property_order', [('root', 'branch'), ('branch', 'root')])
    def test_mutually_recursive_components_refer_to_each_other(self, property_order):
        """Which property reaches the cycle first does not decide which field degrades to ``Any``."""
        signature, _ = _build_tool_signature(_operation(_mutual_recursion_spec(property_order)))

        a = _model(signature.parameters['root'].annotation)
        b = _model(signature.parameters['branch'].annotation)

        assert _model(a.model_fields['b'].annotation) is b
        assert _model(b.model_fields['a'].annotation) is a

    async def test_a_recursive_union_parses_at_every_depth(self, upstream_requests):
        """A cycle through a union, with no model of its own to refer back to, still parses every level."""
        operation = _operation(_union_recursion_spec())
        arguments = {'filter': {'all': ['open', {'all': ['closed', {'all': []}]}]}}

        async with Client(_register(ToolGenerator, operation)) as client:
            result = await client.call_tool('create_thing', arguments)

        assert result.is_error is False
        assert _upstream_body(upstream_requests) == arguments

    def test_distinct_models_have_distinct_names(self):
        """No two generated classes share a name, however the spec's names camelize.

        Two same-named models in one signature leave pydantic to disambiguate them with module-qualified keys.
        """
        signature, _ = _build_tool_signature(_operation(_colliding_names_spec()))

        models = _generated_models(signature)
        names = collections.Counter(model.__name__ for model in models)

        assert len(models) == 23
        assert [name for name, count in names.items() if count > 1] == []

    async def test_colliding_names_still_parse_their_own_fields(self, upstream_requests):
        """Each object reaches the upstream with exactly its own fields, whatever its generated class is called."""
        operation = _operation(_colliding_names_spec())

        async with Client(_register(ToolGenerator, operation)) as client:
            result = await client.call_tool('create_pet', _COLLIDING_NAMES_ARGUMENTS)

        assert result.is_error is False
        assert _upstream_body(upstream_requests) == _COLLIDING_NAMES_ARGUMENTS


class TestOverTheWire:
    """A real client session sees, and is held to, the ``$defs`` form."""

    async def test_defs_survive_to_the_wire(self):
        """The SDK inlines only an output schema whose root is a bare ``$ref``, so nested ``$defs`` reach the client."""
        mcp = _register(ToolGenerator)

        async with Client(mcp) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'create_proposal')

        assert tool.input_schema == build_input_schema(_operation())
        assert tool.input_schema['properties']['payload'] == defs_ref('SourceReference')
        assert set(tool.input_schema['$defs']) == {'SourceReference', 'Node'}

    async def test_nested_arguments_reach_the_upstream_intact(self, upstream_requests):
        """Arguments shaped by a referenced component, recursive ones included, are sent upstream as given."""
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
        (request,) = upstream_requests
        assert dict(request.url.params) == {'status': 'open'}
        assert json.loads(request.content) == {key: value for key, value in arguments.items() if key != 'status'}

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
        """The truncated schema stopped describing ``children`` one level down, the recursive entry checks every level.

        The generated model is recursive too, so the argument parsing catches the missing ``label`` first,
        the same way it catches a missing required field at any other nesting depth.
        """
        mock_upstream(lambda request: pytest.fail('the upstream must not be called'))
        mcp = _register(ToolGenerator)
        tree = {'label': 'root', 'children': [{'label': 'leaf', 'children': [{'children': []}]}]}

        async with Client(mcp) as client:
            result = await client.call_tool('create_proposal', {'payload': {'sourceId': 'docs'}, 'tree': tree})

        assert result.is_error is True
        message = result.content[0]
        assert isinstance(message, TextContent)
        assert 'tree.children.0.children.0.label' in message.text

    async def test_dynamic_exposure_advertises_and_enforces_the_same_schema(self, upstream_requests):
        """``get_operation`` returns the ``$defs`` form, and ``call_operation`` validates against it."""
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
        assert _upstream_body(upstream_requests) == {'payload': {'sourceId': 'docs', 'line': 2}}
        assert rejected.is_error is True


class TestDeriveNameWithResourceOverride:
    """``derive_name`` honors the resource-side override and falls back to the underscored ``operationId``."""

    def test_default_from_operation_id(self):
        """With no override, the name is the underscored, sanitised ``operationId``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=expose_resource(),
        )
        assert derive_name(operation, _resource_override_name(operation)) == 'get_pet'

    def test_override_wins(self):
        """An explicit ``name`` override on the resource opt-in replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=expose_resource(name='pet'),
        )
        assert derive_name(operation, _resource_override_name(operation)) == 'pet'


class TestDeriveDescriptionWithResourceOverride:
    """``derive_description`` falls back through description / summary / ``METHOD /path`` when no override is set."""

    def test_default_from_description(self):
        """The OpenAPI ``description`` is used when set and the override is absent."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            description='Fetch one pet.',
            x_mcp_integration=expose_resource(),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'Fetch one pet.'

    def test_fallback_to_method_path(self):
        """Without description or summary, fall back to ``METHOD /path``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=expose_resource(),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'GET /pets/{petId}'

    def test_override_wins(self):
        """An explicit ``description`` override on the resource opt-in replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            description='Fetch one pet.',
            x_mcp_integration=expose_resource(description='Custom blurb.'),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'Custom blurb.'


def _nested_body_spec() -> dict:
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


def _object(*fields: str) -> dict:
    """An object schema with one string property per name in ``fields``."""
    return {'type': 'object', 'properties': {field: {'type': 'string'} for field in fields}}


def _shared_component_spec() -> dict:
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


def _extended_body_spec(*, person_shared: bool) -> dict:
    """A body that extends ``Base`` through ``allOf``, reaching into properties ``Base`` types by a component.

    The extension adds a field and a required name to ``owner``, a ``Person``,
    and types ``approver``, a ``Reviewer`` in ``Base``, by a second component, ``Admin``.
    ``person_shared`` also reaches ``Person`` from ``watchers``,
    so it is advertised under ``$defs`` rather than inlined.
    """
    base_properties: dict[str, typing.Any] = {'owner': component_ref('Person'), 'approver': component_ref('Reviewer')}
    if person_shared:
        base_properties['watchers'] = {'type': 'array', 'items': component_ref('Person')}
    return operation_spec(
        operation_id='createTicket',
        path='/tickets',
        body={
            'allOf': [
                component_ref('Base'),
                {
                    'required': ['owner'],
                    'properties': {
                        'owner': {
                            'required': ['id'],
                            'properties': {'extra': {'type': 'string', 'description': 'Added by the extension.'}},
                        },
                        'approver': component_ref('Admin'),
                    },
                },
            ]
        },
        schemas={
            'Base': {'type': 'object', 'properties': base_properties},
            'Person': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'id': {'type': 'integer', 'description': 'Person id.'},
                    'name': {'type': 'string', 'description': 'Person name.'},
                },
            },
            'Reviewer': {
                'type': 'object',
                'required': ['name'],
                'properties': {'name': {'type': 'string', 'description': 'Reviewer name.'}},
            },
            'Admin': {
                'type': 'object',
                'required': ['role'],
                'properties': {'role': {'type': 'string', 'enum': ['admin', 'owner'], 'description': 'Admin role.'}},
            },
        },
    )


def _colliding_names_spec() -> dict:
    """A body whose properties and components all map onto a handful of generated model names.

    Each pair below lands on the same ``CreatePet…`` class name under a naive scheme.
    They are an inline object and a component of the same name,
    an inline nested object and a component named after its path, the same for array items and ``anyOf`` variants,
    two nesting paths that camelize alike, two property spellings of one word,
    and five component spellings OpenAPI allows of one name.
    """
    return operation_spec(
        operation_id='createPet',
        path='/pets',
        body={
            'type': 'object',
            'properties': {
                'owner': _object('name'),
                'keeper': component_ref('Owner'),
                'pet': {'type': 'object', 'properties': {'tag': _object('label')}},
                'badge': component_ref('PetTag'),
                'tags': {'type': 'array', 'items': _object('label')},
                'marker': component_ref('TagsItem'),
                'choice': {'anyOf': [_object('left'), _object('right')]},
                'pick': component_ref('ChoiceVariant0'),
                'a_b': {'type': 'object', 'properties': {'c': _object('via_ab')}},
                'a': {'type': 'object', 'properties': {'b_c': _object('via_bc')}},
                'spellings': {
                    'type': 'object',
                    'properties': {'tag_name': _object('snake'), 'tagName': _object('camel')},
                },
                'variants': {
                    'type': 'object',
                    'properties': {
                        'snake': component_ref('foo_bar'),
                        'camel': component_ref('fooBar'),
                        'pascal': component_ref('FooBar'),
                        'kebab': component_ref('foo-bar'),
                        'dotted': component_ref('foo.bar'),
                    },
                },
            },
        },
        schemas={
            'Owner': _object('title'),
            'PetTag': _object('code'),
            'TagsItem': _object('slot'),
            'ChoiceVariant0': _object('pick'),
            'foo_bar': _object('snake'),
            'fooBar': _object('camel'),
            'FooBar': _object('pascal'),
            'foo-bar': _object('kebab'),
            'foo.bar': _object('dotted'),
        },
    )


def _long_names_spec() -> dict:
    """Two components with names past 64 characters, sharing their first 64, each reached twice."""
    long_name = 'ReposCreateOrUpdateEnvironmentRequestBodyDeploymentBranchPolicyCustomBranchPolicies'
    return operation_spec(
        body={
            'type': 'object',
            'properties': {
                'first': component_ref(f'{long_name}Name'),
                'firsts': {'type': 'array', 'items': component_ref(f'{long_name}Name')},
                'second': component_ref(f'{long_name}Pattern'),
                'seconds': {'type': 'array', 'items': component_ref(f'{long_name}Pattern')},
            },
        },
        schemas={
            f'{long_name}Name': {'type': 'object', 'required': ['name'], 'properties': {'name': {'type': 'string'}}},
            f'{long_name}Pattern': {
                'type': 'object',
                'required': ['pattern'],
                'properties': {'pattern': {'type': 'string'}},
            },
        },
    )


def _mutual_recursion_spec(property_order: tuple[str, ...]) -> dict:
    """``A`` and ``B`` refer to each other, and the body reaches ``A`` from ``root`` and ``B`` from ``branch``."""
    properties = {'root': component_ref('A'), 'branch': component_ref('B')}
    return operation_spec(
        body={'type': 'object', 'properties': {name: properties[name] for name in property_order}},
        schemas={
            'A': {'type': 'object', 'properties': {'label': {'type': 'string'}, 'b': component_ref('B')}},
            'B': {'type': 'object', 'properties': {'label': {'type': 'string'}, 'a': component_ref('A')}},
        },
    )


def _union_recursion_spec() -> dict:
    """``Expr`` is a union whose object variant holds a list of ``Expr``.

    The cycle runs through the union rather than through a model of its own.
    """
    return operation_spec(
        body={'type': 'object', 'properties': {'filter': component_ref('Expr')}},
        schemas={
            'Expr': {
                'oneOf': [
                    {'type': 'string', 'enum': ['open', 'closed']},
                    {
                        'type': 'object',
                        'required': ['all'],
                        'properties': {'all': {'type': 'array', 'items': component_ref('Expr')}},
                    },
                ]
            }
        },
    )


def _operation(raw: dict | None = None) -> OperationInfo:
    return parse_spec(raw or _shared_component_spec()).operations[0]


def _register(
    generator_type: type[ToolGenerator] | type[MetaToolGenerator],
    operation: OperationInfo | None = None,
) -> MCPServer:
    mcp = MCPServer('test')
    generator_type(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation or _operation()])
    return mcp


def _advertised(operation: OperationInfo) -> dict:
    """The input schema ``operation`` advertises, checked to hold no reference outside its own ``$defs``."""
    schema = build_input_schema(operation)
    defs = schema.get('$defs', {})
    pending: list[typing.Any] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if '$ref' in node:
                assert node['$ref'].removeprefix('#/$defs/') in defs, f'dangling {node["$ref"]}'
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return schema


def _accepts(schema: dict, arguments: dict) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(arguments)


def _model(annotation: typing.Any) -> type[pydantic.BaseModel]:
    """The one generated model inside ``annotation``, looking through ``Annotated``, ``Optional`` and ``list``."""
    models = _models_in(annotation)
    assert len(models) == 1, models
    return models[0]


def _models_in(annotation: typing.Any) -> list[type[pydantic.BaseModel]]:
    """The generated models ``annotation`` names directly, not those nested inside their fields."""
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return [annotation]
    if isinstance(annotation, (typing.ForwardRef, str)):
        return []
    return [model for argument in typing.get_args(annotation) for model in _models_in(argument)]


def _generated_models(signature: inspect.Signature) -> list[type[pydantic.BaseModel]]:
    """Every distinct model a tool signature generates, at any depth, leaving out the framework's ``Context``."""
    seen: dict[int, type[pydantic.BaseModel]] = {}
    pending = [
        model
        for parameter in signature.parameters.values()
        if parameter.annotation is not Context
        for model in _models_in(parameter.annotation)
    ]
    while pending:
        model = pending.pop()
        if id(model) not in seen:
            seen[id(model)] = model
            pending.extend(sub for field in model.model_fields.values() for sub in _models_in(field.annotation))
    return list(seen.values())


def _upstream_body(upstream_requests: list[httpx.Request]) -> dict:
    (request,) = upstream_requests
    return json.loads(request.content)


def _resource_override_name(operation: OperationInfo) -> str | None:
    """Pull ``resource.name`` off ``operation`` (or ``None``), for passing to :func:`derive_name`."""
    override = operation.x_mcp_integration.resource
    return override.name if override else None


def _resource_override_description(operation: OperationInfo) -> str | None:
    """Pull ``resource.description`` off ``operation`` (or ``None``), for passing to :func:`derive_description`."""
    override = operation.x_mcp_integration.resource
    return override.description if override else None
