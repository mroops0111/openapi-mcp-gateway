import inspect

import httpx
import pytest

from openapi_mcp_gateway.exposure._shaping import shape_operation
from openapi_mcp_gateway.openapi import (
    McpIntegration,
    OperationInfo,
    ParameterInfo,
    ParamOverride,
    ToolOverride,
)
from tests.context import stub_context
from tests.operation import register_tool, shaped_operation


class TestShapeOperation:
    """shape_operation applies the schema-side param overrides purely, returning a shaped copy."""

    def test_hidden_removes_param(self):
        """``hidden`` drops the parameter from the LLM surface."""
        op = shaped_operation(
            [ParameterInfo(name='secret', location='query', required=True, schema={'type': 'string'})],
            params={'secret': {'hidden': True}},
        )
        shaped = shape_operation(op)
        assert [parameter.name for parameter in shaped.parameters] == []

    def test_default_sets_schema_default_and_makes_optional(self):
        """``default`` sets the schema default, flips required off, and marks the parameter for send-on-omit."""
        op = shaped_operation(
            [ParameterInfo(name='limit', location='query', required=True, schema={'type': 'integer'})],
            params={'limit': {'default': 50}},
        )
        shaped = shape_operation(op)
        assert shaped.parameters[0].schema_['default'] == 50
        assert shaped.parameters[0].required is False
        assert shaped.parameters[0].send_default is True

    def test_description_override(self):
        """``description`` replaces the parameter description."""
        op = shaped_operation(
            [ParameterInfo(name='q', location='query', required=True, description='old', schema={'type': 'string'})],
            params={'q': {'description': 'new'}},
        )
        assert shape_operation(op).parameters[0].description == 'new'

    def test_input_operation_not_mutated(self):
        """Shaping is pure, leaving the input operation and its parameter schema untouched."""
        op = shaped_operation(
            [ParameterInfo(name='limit', location='query', required=True, schema={'type': 'integer'})],
            params={'limit': {'default': 50}},
        )
        shape_operation(op)
        assert op.parameters[0].required is True
        assert 'default' not in op.parameters[0].schema_

    def test_no_override_returns_same_operation(self):
        """An operation with no param override is returned unchanged."""
        op = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[ParameterInfo(name='q', location='query', schema={'type': 'string'})],
        )
        assert shape_operation(op) is op


class TestParamShapingSchema:
    """Shaped overrides reach the advertised schema and Python signature of the registered tool."""

    def test_hidden_absent_from_schema_and_signature(self):
        """A hidden param is in neither the advertised schema nor the Python signature."""
        op = shaped_operation(
            [
                ParameterInfo(name='secret', location='query', required=True, schema={'type': 'string'}),
                ParameterInfo(name='visible', location='query', required=True, schema={'type': 'string'}),
            ],
            params={'secret': {'hidden': True}},
        )
        tool = register_tool(op)
        assert 'secret' not in tool.parameters['properties']
        assert 'secret' not in tool.parameters.get('required', [])
        assert 'secret' not in inspect.signature(tool.fn).parameters
        assert 'visible' in tool.parameters['properties']

    def test_default_present_and_optional(self):
        """A default override appears in the schema and drops out of ``required``."""
        op = shaped_operation(
            [ParameterInfo(name='limit', location='query', required=True, schema={'type': 'integer'})],
            params={'limit': {'default': 50}},
        )
        tool = register_tool(op)
        assert tool.parameters['properties']['limit']['default'] == 50
        assert 'limit' not in tool.parameters.get('required', [])

    def test_description_override_in_schema(self):
        """A description override reaches the advertised property schema."""
        op = shaped_operation(
            [ParameterInfo(name='q', location='query', required=True, description='old', schema={'type': 'string'})],
            params={'q': {'description': 'new'}},
        )
        assert register_tool(op).parameters['properties']['q']['description'] == 'new'


class TestHiddenParameterInjection:
    """A hidden parameter with a default is injected upstream while staying out of the LLM surface."""

    def test_hidden_with_default_is_kept_but_not_visible(self):
        """The parameter stays for upstream assembly, marked send_default and not visible."""
        operation = shaped_operation(
            [ParameterInfo(name='format', location='path', required=True, schema={'type': 'string'})],
            params={'format': {'hidden': True, 'default': 'json'}},
            params_strategy='merge',
            path='/issues.{format}',
        )
        shaped = shape_operation(operation)
        format_parameter = next(parameter for parameter in shaped.parameters if parameter.name == 'format')
        assert format_parameter.visible is False
        assert format_parameter.send_default is True
        assert 'format' not in register_tool(operation).parameters['properties']

    async def test_hidden_default_fills_the_upstream_path(self, mock_upstream):
        """The default fills the path placeholder even though the LLM never supplies the parameter."""
        captured_request: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request['path'] = request.url.path
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        operation = shaped_operation(
            [ParameterInfo(name='format', location='path', required=True, schema={'type': 'string'})],
            params={'format': {'hidden': True, 'default': 'json'}},
            params_strategy='merge',
            path='/issues.{format}',
        )
        await register_tool(operation).run({}, context=stub_context())
        assert captured_request['path'] == '/issues.json'

    def test_hidden_required_path_parameter_without_default_raises(self):
        """Hiding a required path parameter with no default and no request cannot fill the path."""
        operation = shaped_operation(
            [ParameterInfo(name='format', location='path', required=True, schema={'type': 'string'})],
            params={'format': {'hidden': True}},
            params_strategy='merge',
            path='/issues.{format}',
        )
        with pytest.raises(ValueError, match='nothing to fill'):
            shape_operation(operation)


class TestParamDeclaration:
    """Declared params (carrying a ``type``) rebuild the LLM input schema and drop the spec parameters."""

    def test_declared_params_replace_spec_params(self):
        """When params declare a schema, the tool advertises only the declared params, not the spec's."""
        op = shaped_operation(
            [ParameterInfo(name='sort_by', location='query', required=True, schema={'type': 'string'})],
            params={
                'sort': {'type': 'string', 'enum': ['popular', 'newest'], 'default': 'popular'},
                'page': {'type': 'integer'},
            },
            request='{"sort_by": sort, "page": page}',
        )
        tool = register_tool(op)
        props = tool.parameters['properties']
        assert set(props) == {'sort', 'page'}
        assert props['sort']['enum'] == ['popular', 'newest']
        assert props['sort']['default'] == 'popular'
        assert props['page']['type'] == 'integer'

    def test_declared_required_flag(self):
        """``required: true`` lifts a declared param into the schema's required list."""
        op = shaped_operation(
            [],
            params={'movie_id': {'type': 'integer', 'required': True}},
            request='{"movie_id": movie_id}',
            path='/movie/{movie_id}',
        )
        assert register_tool(op).parameters['required'] == ['movie_id']

    def test_declared_description_reaches_schema_and_signature(self):
        """A declared param's description reaches the schema and the param appears on the signature."""
        op = shaped_operation(
            [],
            params={'q': {'type': 'string', 'required': True, 'description': 'Search text.'}},
            request='{"query": q}',
        )
        tool = register_tool(op)
        assert tool.parameters['properties']['q']['description'] == 'Search text.'
        assert 'q' in inspect.signature(tool.fn).parameters

    async def test_declared_param_maps_through_request(self, mock_upstream):
        """A declared friendly param reaches the upstream under the name the request expression gives it."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='sort_by', location='query', required=False, schema={'type': 'string'})],
            params={'sort': {'type': 'string', 'enum': ['popular'], 'default': 'popular'}},
            request='{"sort_by": $lookup({"popular": "popularity.desc"}, sort)}',
        )
        await register_tool(op).run({}, context=stub_context())
        assert captured['params']['sort_by'] == 'popularity.desc'
        assert 'sort' not in captured['params']

    def test_declared_without_request_fails_at_registration(self):
        """Declaring params without a request expression is rejected at build time."""
        op = shaped_operation([], params={'q': {'type': 'string'}})
        with pytest.raises(ValueError, match='request expression'):
            register_tool(op)


class TestStrategy:
    """``params_strategy`` explicitly chooses merge (layer onto spec) or replace (declare the whole surface)."""

    def test_missing_strategy_raises(self):
        """Setting params without a strategy is rejected at build time."""
        op = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[ParameterInfo(name='q', location='query', schema={'type': 'string'})],
            x_mcp_integration=McpIntegration(tool=ToolOverride(params={'q': ParamOverride(hidden=True)})),
        )
        with pytest.raises(ValueError, match=r'tool\.params_strategy'):
            shape_operation(op)

    def test_merge_keeps_undeclared_spec_params(self):
        """Under merge, a spec parameter the override does not mention stays visible."""
        op = shaped_operation(
            [
                ParameterInfo(name='keep', location='query', required=True, schema={'type': 'string'}),
                ParameterInfo(name='secret', location='query', required=True, schema={'type': 'string'}),
            ],
            params={'secret': {'hidden': True}},
            params_strategy='merge',
        )
        assert [parameter.name for parameter in shape_operation(op).parameters] == ['keep']

    def test_merge_rejects_unknown_param(self):
        """Under merge, naming a parameter the spec does not define is rejected, pointing at replace."""
        op = shaped_operation(
            [ParameterInfo(name='keep', location='query', schema={'type': 'string'})],
            params={'extra': {'type': 'string'}},
            params_strategy='merge',
        )
        with pytest.raises(ValueError, match='replace'):
            shape_operation(op)

    def test_merge_skips_a_tweak_of_a_left_out_parameter(self, caplog):
        """Under merge, a tweak naming a parameter the gateway left out is skipped with a warning, not rejected."""
        operation = shaped_operation(
            [ParameterInfo(name='keep', location='query', schema={'type': 'string'})],
            params={'expand': {'hidden': True}},
            params_strategy='merge',
            unsendable_query_parameters=['expand'],
        )
        assert [parameter.name for parameter in shape_operation(operation).parameters] == ['keep']
        assert "['expand']" in caplog.text

    def test_replace_drops_undeclared_spec_params(self):
        """Under replace, every spec parameter the override does not declare is dropped."""
        op = shaped_operation(
            [ParameterInfo(name='raw', location='query', required=True, schema={'type': 'string'})],
            params={'friendly': {'type': 'string'}},
            params_strategy='replace',
            request='{"raw": friendly}',
        )
        assert [parameter.name for parameter in shape_operation(op).parameters] == ['friendly']

    def test_merge_overrides_matching_param_schema(self):
        """Under merge, a typed entry replaces the matching spec parameter's schema."""
        op = shaped_operation(
            [ParameterInfo(name='sort', location='query', required=True, schema={'type': 'string'})],
            params={'sort': {'type': 'string', 'enum': ['a', 'b']}},
            params_strategy='merge',
        )
        shaped = shape_operation(op)
        assert shaped.parameters[0].schema_['enum'] == ['a', 'b']
