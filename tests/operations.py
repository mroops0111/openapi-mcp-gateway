import collections.abc

from openapi_mcp_gateway.openapi import OperationInfo


def operation(
    operation_id: str = 'listPets',
    method: str = 'get',
    path: str = '/pets',
    tags: collections.abc.Sequence[str] = ('pets',),
    security: collections.abc.Sequence[dict[str, list[str]]] = (),
) -> OperationInfo:
    """An operation as the spec parser produces it, with only what scopes read from it."""
    return OperationInfo(operation_id=operation_id, method=method, path=path, tags=list(tags), security=list(security))


# The petstore fixture's operations as scopes see them, tagged and untagged alike.
PETSTORE_OPERATIONS = (
    operation('listPets', 'get', '/pets'),
    operation('createPet', 'post', '/pets'),
    operation('getPetById', 'get', '/pets/{petId}'),
    operation('deletePet', 'delete', '/pets/{petId}'),
    operation('createPetWithOwner', 'post', '/pets/with-owner', tags=()),
    operation('adminListPets', 'get', '/admin/pets', tags=()),
)
