import json
import types
import typing

from openapi_mcp_gateway import Gateway
from openapi_mcp_gateway.exposure._shared import (
    _sanitize_name,
    _schema_to_python_type,
    build_input_schema,
)
from tests.specs import operation_spec


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
