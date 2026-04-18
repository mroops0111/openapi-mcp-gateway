"""Shared fixtures for the test suite."""

import fakeredis.aioredis
import pytest_asyncio

from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from openapi_mcp_gateway.stores.redis import RedisTokenStore


@pytest_asyncio.fixture
async def memory_store():
    store = MemoryTokenStore()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def redis_store():
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTokenStore.__new__(RedisTokenStore)
    store._prefix = 'test'
    store._redis = fake_redis
    yield store
    await store.close()


PETSTORE_SPEC_RAW = {
    'openapi': '3.0.0',
    'info': {'title': 'Petstore', 'version': '1.0.0'},
    'servers': [{'url': 'https://petstore.example.com/v1'}],
    'components': {
        'schemas': {
            'Pet': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'id': {'type': 'integer'},
                    'name': {'type': 'string'},
                    'tag': {'type': 'string'},
                },
            },
            'Error': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'integer'},
                    'message': {'type': 'string'},
                },
            },
            'PetWithOwner': {
                'allOf': [
                    {'$ref': '#/components/schemas/Pet'},
                    {
                        'type': 'object',
                        'required': ['owner'],
                        'properties': {
                            'owner': {'type': 'string'},
                        },
                    },
                ],
            },
            'PetOrError': {
                'oneOf': [
                    {'$ref': '#/components/schemas/Pet'},
                    {'$ref': '#/components/schemas/Error'},
                ],
            },
            'MaybePet': {
                'anyOf': [
                    {'$ref': '#/components/schemas/Pet'},
                    {'type': 'null'},
                ],
            },
            'NestedOwner': {
                'type': 'object',
                'properties': {
                    'pet': {'$ref': '#/components/schemas/Pet'},
                    'pets': {'type': 'array', 'items': {'$ref': '#/components/schemas/Pet'}},
                },
            },
        },
        'parameters': {
            'LimitParam': {
                'name': 'limit',
                'in': 'query',
                'required': False,
                'description': 'Max items to return',
                'schema': {'type': 'integer'},
            },
        },
        'securitySchemes': {
            'oauth2': {
                'type': 'oauth2',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': 'https://auth.example.com/authorize',
                        'tokenUrl': 'https://auth.example.com/token',
                        'scopes': {
                            'read:pets': 'Read pets',
                            'write:pets': 'Write pets',
                        },
                    },
                },
            },
        },
    },
    'paths': {
        '/pets': {
            'parameters': [
                {'$ref': '#/components/parameters/LimitParam'},
            ],
            'get': {
                'operationId': 'listPets',
                'summary': 'List all pets',
                'tags': ['pets'],
                'parameters': [
                    {
                        'name': 'limit',
                        'in': 'query',
                        'required': False,
                        'schema': {'type': 'integer'},
                    },
                ],
                'responses': {'200': {'description': 'OK'}},
            },
            'post': {
                'operationId': 'createPet',
                'summary': 'Create a pet',
                'tags': ['pets'],
                'requestBody': {
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/Pet'},
                        },
                    },
                },
                'responses': {'201': {'description': 'Created'}},
            },
        },
        '/pets/{petId}': {
            'get': {
                'operationId': 'getPetById',
                'summary': 'Get a pet by ID',
                'tags': ['pets'],
                'parameters': [
                    {
                        'name': 'petId',
                        'in': 'path',
                        'required': True,
                        'schema': {'type': 'integer'},
                    },
                ],
                'responses': {'200': {'description': 'OK'}},
            },
            'delete': {
                'operationId': 'deletePet',
                'summary': 'Delete a pet',
                'tags': ['pets'],
                'parameters': [
                    {
                        'name': 'petId',
                        'in': 'path',
                        'required': True,
                        'schema': {'type': 'integer'},
                    },
                ],
                'responses': {'204': {'description': 'Deleted'}},
            },
        },
        '/pets/with-owner': {
            'post': {
                'operationId': 'createPetWithOwner',
                'summary': 'Create a pet with owner',
                'requestBody': {
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/PetWithOwner'},
                        },
                    },
                },
                'responses': {'201': {'description': 'Created'}},
            },
        },
        '/admin/pets': {
            'get': {
                'operationId': 'adminListPets',
                'summary': 'Admin list pets',
                'x-mcp-integration': {'expose': {'tool': {}}},
                'responses': {'200': {'description': 'OK'}},
            },
        },
    },
}
