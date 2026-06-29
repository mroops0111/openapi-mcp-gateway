import json

import pytest

from openapi_mcp_gateway.openapi import (
    _deep_merge,
    _expand_schema,
    _resolve_ref,
    load_spec,
    parse_spec,
)


class TestResolveRef:
    """JSON-pointer ``$ref`` resolution against a parsed spec document."""

    def test_simple_ref(self, petstore_spec_raw):
        """A direct schema ref resolves to the target node."""
        result = _resolve_ref(petstore_spec_raw, '#/components/schemas/Pet')
        assert result['type'] == 'object'
        assert 'name' in result['properties']

    def test_nested_ref(self, petstore_spec_raw):
        """A nested ref through ``components/parameters`` resolves correctly."""
        result = _resolve_ref(petstore_spec_raw, '#/components/parameters/LimitParam')
        assert result['name'] == 'limit'

    def test_bad_ref_returns_empty(self, petstore_spec_raw):
        """A ref to a non-existent schema returns an empty dict instead of raising."""
        result = _resolve_ref(petstore_spec_raw, '#/components/schemas/DoesNotExist')
        assert result == {}

    def test_bad_path_returns_empty(self, petstore_spec_raw):
        """A ref through a non-existent intermediate node returns an empty dict."""
        result = _resolve_ref(petstore_spec_raw, '#/foo/bar/baz')
        assert result == {}


class TestDeepMerge:
    """Recursive dict merge used to combine ``allOf`` branches."""

    def test_basic_merge(self):
        """Two flat dicts merge into the union of their keys."""
        result = _deep_merge({'a': 1}, {'b': 2})
        assert result == {'a': 1, 'b': 2}

    def test_nested_merge(self):
        """Nested dicts merge recursively; later values override earlier."""
        result = _deep_merge(
            {'props': {'a': 1, 'b': 2}},
            {'props': {'b': 3, 'c': 4}},
        )
        assert result == {'props': {'a': 1, 'b': 3, 'c': 4}}

    def test_required_concatenation(self):
        """``required`` lists are concatenated and deduplicated, preserving order."""
        result = _deep_merge(
            {'required': ['id', 'name']},
            {'required': ['name', 'owner']},
        )
        assert result['required'] == ['id', 'name', 'owner']

    def test_override_scalar(self):
        """Scalar values on the right-hand side override the left-hand side."""
        result = _deep_merge({'type': 'string'}, {'type': 'integer'})
        assert result['type'] == 'integer'


class TestExpandSchema:
    """``$ref`` / ``allOf`` / ``oneOf`` / ``anyOf`` expansion on schema nodes."""

    def test_direct_ref(self, petstore_spec_raw):
        """A bare ``$ref`` is replaced by the resolved schema."""
        schema = {'$ref': '#/components/schemas/Pet'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result['type'] == 'object'
        assert 'name' in result['properties']

    def test_allof_merge(self, petstore_spec_raw):
        """``allOf`` branches are merged into a single schema with combined required."""
        schema = {'$ref': '#/components/schemas/PetWithOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert 'id' in result['properties']
        assert 'name' in result['properties']
        assert 'owner' in result['properties']
        assert set(result['required']) == {'name', 'owner'}

    def test_oneof(self, petstore_spec_raw):
        """``oneOf`` branches are kept as a list with each branch independently expanded."""
        schema = {'$ref': '#/components/schemas/PetOrError'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert len(result['oneOf']) == 2
        assert result['oneOf'][0]['properties']['name']['type'] == 'string'
        assert result['oneOf'][1]['properties']['code']['type'] == 'integer'

    def test_anyof(self, petstore_spec_raw):
        """``anyOf`` branches expand independently, including non-object branches like ``null``."""
        schema = {'$ref': '#/components/schemas/MaybePet'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert len(result['anyOf']) == 2
        assert result['anyOf'][0]['properties']['name']['type'] == 'string'
        assert result['anyOf'][1] == {'type': 'null'}

    def test_nested_object_properties(self, petstore_spec_raw):
        """Nested object properties are recursively expanded."""
        schema = {'$ref': '#/components/schemas/NestedOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result['properties']['pet']['type'] == 'object'
        assert 'name' in result['properties']['pet']['properties']

    def test_nested_array_items(self, petstore_spec_raw):
        """Array ``items`` schemas are recursively expanded."""
        schema = {'$ref': '#/components/schemas/NestedOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result['properties']['pets']['type'] == 'array'
        assert result['properties']['pets']['items']['type'] == 'object'
        assert 'name' in result['properties']['pets']['items']['properties']

    def test_plain_schema_unchanged(self, petstore_spec_raw):
        """A schema without refs or composite keywords is returned unchanged."""
        schema = {'type': 'string', 'description': 'A name'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result == schema

    def test_inline_allof(self, petstore_spec_raw):
        """Inline ``allOf`` (mixing refs and literal schemas) merges correctly."""
        schema = {
            'allOf': [
                {'$ref': '#/components/schemas/Pet'},
                {'type': 'object', 'properties': {'color': {'type': 'string'}}},
            ],
        }
        result = _expand_schema(petstore_spec_raw, schema)
        assert 'name' in result['properties']
        assert 'color' in result['properties']


class TestParseSpec:
    """End-to-end ``parse_spec`` against the petstore fixture."""

    @pytest.fixture(autouse=True)
    def _setup(self, petstore_spec_raw):
        """Parse the petstore fixture once per test method."""
        self.spec = parse_spec(petstore_spec_raw)

    def test_metadata(self):
        """Title, version, and default base URL are surfaced from the spec."""
        assert self.spec.title == 'Petstore'
        assert self.spec.version == '1.0.0'
        assert self.spec.default_base_url == 'https://petstore.example.com/v1'

    def test_operation_count(self):
        """All declared operations show up by id."""
        ids = [op.operation_id for op in self.spec.operations]
        assert 'listPets' in ids
        assert 'createPet' in ids
        assert 'getPetById' in ids
        assert 'deletePet' in ids
        assert 'createPetWithOwner' in ids
        assert 'adminListPets' in ids

    def test_openapi_operation_methods(self):
        """Valid OpenAPI operation methods beyond the common five are parsed."""
        raw = {
            'openapi': '3.0.0',
            'info': {'title': 'Methods', 'version': '1.0'},
            'paths': {
                '/status': {
                    'head': {'operationId': 'checkStatusHead', 'responses': {'200': {'description': 'OK'}}},
                    'options': {'operationId': 'checkStatusOptions', 'responses': {'200': {'description': 'OK'}}},
                    'trace': {'operationId': 'traceStatus', 'responses': {'200': {'description': 'OK'}}},
                },
            },
        }
        spec = parse_spec(raw)
        methods_by_id = {operation.operation_id: operation.method for operation in spec.operations}
        assert methods_by_id == {
            'checkStatusHead': 'head',
            'checkStatusOptions': 'options',
            'traceStatus': 'trace',
        }

    def test_param_dedup(self):
        """``listPets`` has ``limit`` at both path-level and operation-level, should dedup."""
        list_pets = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        limit_params = [param for param in list_pets.parameters if param.name == 'limit']
        assert len(limit_params) == 1

    def test_ref_parameter_resolved(self):
        """Path-level ``$ref`` parameters (``LimitParam``) are resolved into ``ParameterInfo``."""
        list_pets = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        limit = next(param for param in list_pets.parameters if param.name == 'limit')
        assert limit.location == 'query'
        assert limit.schema_type == 'integer'

    def test_request_body_expanded(self):
        """``createPet`` body schema becomes individual body params with required flags."""
        create_pet = next(op for op in self.spec.operations if op.operation_id == 'createPet')
        body_params = [param for param in create_pet.parameters if param.location == 'body']
        names = {param.name for param in body_params}
        assert names == {'id', 'name', 'tag'}
        name_param = next(param for param in body_params if param.name == 'name')
        assert name_param.required is True

    def test_allof_request_body_expanded(self):
        """``createPetWithOwner`` body uses ``allOf``, should merge ``Pet`` and owner."""
        operation = next(op for op in self.spec.operations if op.operation_id == 'createPetWithOwner')
        body_params = [param for param in operation.parameters if param.location == 'body']
        names = {param.name for param in body_params}
        assert 'id' in names
        assert 'name' in names
        assert 'owner' in names

    def test_x_mcp_integration(self):
        """``x-mcp`` extension on an operation flips ``tool_exposed`` accordingly."""
        admin_op = next(op for op in self.spec.operations if op.operation_id == 'adminListPets')
        assert admin_op.tool_exposed is True

        list_op = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        assert list_op.tool_exposed is False

    def test_x_mcp_integration_tool_override_parsed(self):
        """``expose.tool.name`` and ``description`` reach the typed model."""
        admin_op = next(op for op in self.spec.operations if op.operation_id == 'adminListPets')
        assert admin_op.x_mcp_integration.expose is not None
        override = admin_op.x_mcp_integration.expose.tool
        assert override is not None
        assert override.name == 'listAdminPets'
        assert override.description == 'List pets visible only to admin users.'

    def test_generated_operation_id(self):
        """Operations without ``operationId`` get one synthesised from method+path."""
        raw = {
            'openapi': '3.0.0',
            'info': {'title': 'Test', 'version': '1.0'},
            'paths': {
                '/users/{id}': {
                    'get': {
                        'responses': {'200': {'description': 'OK'}},
                    },
                },
            },
        }
        spec = parse_spec(raw)
        assert spec.operations[0].operation_id == 'get__users_id'

    def test_security_schemes_parsed(self):
        """Top-level ``components.securitySchemes`` is exposed on the parsed spec."""
        assert 'oauth2' in self.spec.security_schemes


class TestRelativeServerResolution:
    """Resolution of relative ``servers[].url`` against the spec source URL."""

    @pytest.fixture
    def relative_spec_raw(self):
        """Minimal spec whose server URL is a path-only ``/api/v3``."""
        return {
            'openapi': '3.0.0',
            'info': {'title': 'T', 'version': '1'},
            'servers': [{'url': '/api/v3'}],
            'paths': {},
        }

    def test_resolves_path_against_url_source(self, relative_spec_raw):
        """A relative server URL is resolved against the HTTP source URL."""
        spec = parse_spec(relative_spec_raw, source='https://petstore3.swagger.io/api/v3/openapi.json')
        assert spec.default_base_url == 'https://petstore3.swagger.io/api/v3'

    def test_leaves_absolute_url_alone(self):
        """An absolute server URL is preserved regardless of source."""
        raw = {
            'openapi': '3.0.0',
            'info': {'title': 'T', 'version': '1'},
            'servers': [{'url': 'https://api.example.com/v1'}],
            'paths': {},
        }
        spec = parse_spec(raw, source='https://other.com/spec.json')
        assert spec.default_base_url == 'https://api.example.com/v1'

    def test_no_source_passes_through(self, relative_spec_raw):
        """Without a source URL, a relative server URL is left as-is."""
        spec = parse_spec(relative_spec_raw)
        assert spec.default_base_url == '/api/v3'

    def test_local_file_source_passes_through(self, relative_spec_raw, tmp_path):
        """A local file source does not act as a base for relative server URLs."""
        spec = parse_spec(relative_spec_raw, source=str(tmp_path / 'spec.json'))
        assert spec.default_base_url == '/api/v3'


class TestLoadSpec:
    """File loading for both JSON and YAML spec formats."""

    def test_load_json(self, petstore_json_path):
        """A ``.json`` spec file is loaded into a dict with parsed metadata."""
        raw = load_spec(petstore_json_path)
        assert raw['info']['title'] == 'Petstore'

    def test_load_yaml(self, petstore_yml_path):
        """A ``.yml`` spec file is loaded equivalently to JSON."""
        raw = load_spec(petstore_yml_path)
        assert raw['info']['title'] == 'Petstore'

    def test_json_and_yaml_equivalent(self, petstore_json_path, petstore_yml_path):
        """JSON and YAML versions of the same spec produce identical dicts."""
        json_raw = load_spec(petstore_json_path)
        yaml_raw = load_spec(petstore_yml_path)
        assert json_raw == yaml_raw

    def test_file_not_found(self, tmp_path):
        """A missing local file surfaces as ``FileNotFoundError``."""
        with pytest.raises(FileNotFoundError):
            load_spec(tmp_path / 'nonexistent.json')

    def test_malformed_json_raises(self, tmp_path):
        """A ``.json`` file with invalid JSON surfaces as ``json.JSONDecodeError``."""
        bad_json = tmp_path / 'bad.json'
        bad_json.write_text('{not valid json')
        with pytest.raises(json.JSONDecodeError):
            load_spec(bad_json)
