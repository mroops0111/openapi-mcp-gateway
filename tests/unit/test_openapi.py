"""Tests for OpenAPI spec parsing and schema expansion."""

import pytest

from openapi_mcp_gateway.openapi import (
    _deep_merge,
    _expand_schema,
    _resolve_ref,
    load_spec,
    parse_spec,
)


class TestResolveRef:
    def test_simple_ref(self, petstore_spec_raw):
        result = _resolve_ref(petstore_spec_raw, '#/components/schemas/Pet')
        assert result['type'] == 'object'
        assert 'name' in result['properties']

    def test_nested_ref(self, petstore_spec_raw):
        result = _resolve_ref(petstore_spec_raw, '#/components/parameters/LimitParam')
        assert result['name'] == 'limit'

    def test_bad_ref_returns_empty(self, petstore_spec_raw):
        result = _resolve_ref(petstore_spec_raw, '#/components/schemas/DoesNotExist')
        assert result == {}

    def test_bad_path_returns_empty(self, petstore_spec_raw):
        result = _resolve_ref(petstore_spec_raw, '#/foo/bar/baz')
        assert result == {}


class TestDeepMerge:
    def test_basic_merge(self):
        result = _deep_merge({'a': 1}, {'b': 2})
        assert result == {'a': 1, 'b': 2}

    def test_nested_merge(self):
        result = _deep_merge(
            {'props': {'a': 1, 'b': 2}},
            {'props': {'b': 3, 'c': 4}},
        )
        assert result == {'props': {'a': 1, 'b': 3, 'c': 4}}

    def test_required_concatenation(self):
        result = _deep_merge(
            {'required': ['id', 'name']},
            {'required': ['name', 'owner']},
        )
        assert result['required'] == ['id', 'name', 'owner']

    def test_override_scalar(self):
        result = _deep_merge({'type': 'string'}, {'type': 'integer'})
        assert result['type'] == 'integer'


class TestExpandSchema:
    def test_direct_ref(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/Pet'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result['type'] == 'object'
        assert 'name' in result['properties']

    def test_allof_merge(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/PetWithOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert 'id' in result['properties']
        assert 'name' in result['properties']
        assert 'owner' in result['properties']
        assert set(result['required']) == {'name', 'owner'}

    def test_oneof(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/PetOrError'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert len(result['oneOf']) == 2
        assert result['oneOf'][0]['properties']['name']['type'] == 'string'
        assert result['oneOf'][1]['properties']['code']['type'] == 'integer'

    def test_anyof(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/MaybePet'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert len(result['anyOf']) == 2
        assert result['anyOf'][0]['properties']['name']['type'] == 'string'
        assert result['anyOf'][1] == {'type': 'null'}

    def test_nested_object_properties(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/NestedOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        # pet property should be expanded
        assert result['properties']['pet']['type'] == 'object'
        assert 'name' in result['properties']['pet']['properties']

    def test_nested_array_items(self, petstore_spec_raw):
        schema = {'$ref': '#/components/schemas/NestedOwner'}
        result = _expand_schema(petstore_spec_raw, schema)
        # pets array items should be expanded
        assert result['properties']['pets']['type'] == 'array'
        assert result['properties']['pets']['items']['type'] == 'object'
        assert 'name' in result['properties']['pets']['items']['properties']

    def test_plain_schema_unchanged(self, petstore_spec_raw):
        schema = {'type': 'string', 'description': 'A name'}
        result = _expand_schema(petstore_spec_raw, schema)
        assert result == schema

    def test_inline_allof(self, petstore_spec_raw):
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
    @pytest.fixture(autouse=True)
    def _setup(self, petstore_spec_raw):
        self.spec = parse_spec(petstore_spec_raw)

    def test_metadata(self):
        assert self.spec.title == 'Petstore'
        assert self.spec.version == '1.0.0'
        assert self.spec.default_base_url == 'https://petstore.example.com/v1'

    def test_operation_count(self):
        ids = [op.operation_id for op in self.spec.operations]
        assert 'listPets' in ids
        assert 'createPet' in ids
        assert 'getPetById' in ids
        assert 'deletePet' in ids
        assert 'createPetWithOwner' in ids
        assert 'adminListPets' in ids

    def test_param_dedup(self):
        """listPets has 'limit' at both path-level and operation-level — should dedup."""
        list_pets = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        limit_params = [p for p in list_pets.parameters if p.name == 'limit']
        assert len(limit_params) == 1

    def test_ref_parameter_resolved(self):
        """Path-level $ref param (LimitParam) should be resolved."""
        list_pets = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        limit = next(p for p in list_pets.parameters if p.name == 'limit')
        assert limit.location == 'query'
        assert limit.schema_type == 'integer'

    def test_request_body_expanded(self):
        """createPet body should have Pet properties as body params."""
        create_pet = next(op for op in self.spec.operations if op.operation_id == 'createPet')
        body_params = [p for p in create_pet.parameters if p.location == 'body']
        names = {p.name for p in body_params}
        assert names == {'id', 'name', 'tag'}
        name_param = next(p for p in body_params if p.name == 'name')
        assert name_param.required is True

    def test_allof_request_body_expanded(self):
        """createPetWithOwner body uses allOf — should merge Pet + owner."""
        op = next(o for o in self.spec.operations if o.operation_id == 'createPetWithOwner')
        body_params = [p for p in op.parameters if p.location == 'body']
        names = {p.name for p in body_params}
        assert 'id' in names
        assert 'name' in names
        assert 'owner' in names

    def test_x_mcp_integration(self):
        admin_op = next(op for op in self.spec.operations if op.operation_id == 'adminListPets')
        assert admin_op.tool_exposed is True

        list_op = next(op for op in self.spec.operations if op.operation_id == 'listPets')
        assert list_op.tool_exposed is False

    def test_generated_operation_id(self):
        """Operations without operationId should get one generated."""
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
        assert 'oauth2' in self.spec.security_schemes


class TestRelativeServerResolution:
    @pytest.fixture
    def relative_spec_raw(self):
        return {
            'openapi': '3.0.0',
            'info': {'title': 'T', 'version': '1'},
            'servers': [{'url': '/api/v3'}],
            'paths': {},
        }

    def test_resolves_path_against_url_source(self, relative_spec_raw):
        spec = parse_spec(relative_spec_raw, source='https://petstore3.swagger.io/api/v3/openapi.json')
        assert spec.default_base_url == 'https://petstore3.swagger.io/api/v3'

    def test_leaves_absolute_url_alone(self):
        raw = {
            'openapi': '3.0.0',
            'info': {'title': 'T', 'version': '1'},
            'servers': [{'url': 'https://api.example.com/v1'}],
            'paths': {},
        }
        spec = parse_spec(raw, source='https://other.com/spec.json')
        assert spec.default_base_url == 'https://api.example.com/v1'

    def test_no_source_passes_through(self, relative_spec_raw):
        spec = parse_spec(relative_spec_raw)
        assert spec.default_base_url == '/api/v3'

    def test_local_file_source_passes_through(self, relative_spec_raw, tmp_path):
        spec = parse_spec(relative_spec_raw, source=str(tmp_path / 'spec.json'))
        assert spec.default_base_url == '/api/v3'


class TestLoadSpec:
    def test_load_json(self, petstore_json_path):
        raw = load_spec(petstore_json_path)
        assert raw['info']['title'] == 'Petstore'

    def test_load_yaml(self, petstore_yml_path):
        raw = load_spec(petstore_yml_path)
        assert raw['info']['title'] == 'Petstore'

    def test_json_and_yaml_equivalent(self, petstore_json_path, petstore_yml_path):
        json_raw = load_spec(petstore_json_path)
        yaml_raw = load_spec(petstore_yml_path)
        assert json_raw == yaml_raw

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_spec(tmp_path / 'nonexistent.json')
