import dataclasses
import typing

from mcp.types import CallToolResult

from ..openapi import OperationInfo, ParameterInfo
from ._query_string import (
    FormQuerySerializer,
    QueryPair,
    QuerySerializer,
    UndeclaredQuerySerializer,
    encode_query_string,
)
from ._shared import sanitize_name, split_by_location
from ._tool_result import build_query_override_error_result


@dataclasses.dataclass(frozen=True)
class UpstreamArguments:
    """The arguments an upstream request is built from, keyed by spec name, as request shaping hands them over.

    ``path``, ``query``, ``headers`` and ``body`` hold arguments passed through for the parameter each fills.
    ``unlocated`` holds the result of a ``request`` expression, which the builder places by the spec.
    """

    path: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    query: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    headers: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    body: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    unlocated: dict[str, typing.Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class UpstreamRequest:
    """One upstream request, laid out as the spec declares it and ready to send."""

    path: str
    # The encoded query string, without the leading ``?``.
    query_string: str
    headers: dict[str, str]
    body: dict[str, typing.Any]


class UpstreamRequestBuilder:
    """Lay the upstream arguments of one operation out as the request its spec declares.

    It is built from the operation before shaping, since shaping changes what the LLM sees, not what the upstream takes.
    """

    # The serializer behind each query style the parser keeps.
    # A new style is a new ``QuerySerializer`` here and a new entry in ``openapi``'s sendable query styles.
    _QUERY_SERIALIZERS_BY_STYLE: typing.ClassVar[dict[str, QuerySerializer]] = {
        'form': FormQuerySerializer(),
    }
    # The serializer of every key a request expression adds that no query parameter of the spec declares.
    _UNDECLARED_QUERY_SERIALIZER: typing.ClassVar[QuerySerializer] = UndeclaredQuerySerializer()

    def __init__(self, spec_operation: OperationInfo) -> None:
        self._method = spec_operation.method
        self._path = spec_operation.path
        _, query_parameters, header_parameters, _ = split_by_location(spec_operation.parameters)
        # The spec's query parameters by spec name, whose style and explode the query string follows.
        self._query_parameters = {parameter.name: parameter for parameter in query_parameters}
        # The spec name of each header and query parameter, under its spec name and its sanitised name,
        # since a ``request`` expression may name it by either.
        self._header_spec_names = _spec_names_by_either_name(header_parameters)
        self._query_spec_names = _spec_names_by_either_name(query_parameters)

    def build_request(self, upstream_arguments: UpstreamArguments) -> UpstreamRequest | CallToolResult:
        """Build the request, or the error result of an object property that would override another parameter."""
        located = self._locate(upstream_arguments.unlocated)
        path = self._path
        for name, value in {**upstream_arguments.path, **located.path}.items():
            path = path.replace(f'{{{name}}}', str(value))
        query_string = self._encode_query_string({**upstream_arguments.query, **located.query})
        if isinstance(query_string, CallToolResult):
            return query_string
        return UpstreamRequest(
            path=path,
            query_string=query_string,
            headers={name: str(value) for name, value in {**upstream_arguments.headers, **located.headers}.items()},
            body={**upstream_arguments.body, **located.body},
        )

    def _locate(self, unlocated: dict[str, typing.Any]) -> UpstreamArguments:
        """Place a ``request`` expression's result by the spec, dropping ``None`` values.

        A key that names a path-template placeholder fills the path.
        A key that names a header or query parameter, by spec or sanitised name, goes there under its spec name.
        Every other key becomes a query parameter for a body-less method (GET, DELETE, HEAD),
        or a JSON body field otherwise.
        """
        located = UpstreamArguments()
        method_has_body = self._method.lower() in ('post', 'put', 'patch')
        for key, value in unlocated.items():
            if value is None:
                continue
            if f'{{{key}}}' in self._path:
                located.path[key] = value
            elif key in self._header_spec_names:
                located.headers[self._header_spec_names[key]] = value
            elif key in self._query_spec_names:
                located.query[self._query_spec_names[key]] = value
            elif method_has_body:
                located.body[key] = value
            else:
                located.query[key] = value
        return located

    def _encode_query_string(self, query_arguments: dict[str, typing.Any]) -> str | CallToolResult:
        """Encode query arguments keyed by spec name as the query string, without the leading ``?``.

        An exploded object spreads into its own property names,
        so one that names another query parameter is refused rather than allowed to override it.
        """
        query_pairs: list[QueryPair] = []
        overriding_properties: list[str] = []
        for name, value in query_arguments.items():
            for pair in self._query_pairs(name, value):
                if pair.name != name and pair.name in self._query_parameters:
                    overriding_properties.append(f'{name}.{pair.name}')
                query_pairs.append(pair)
        if overriding_properties:
            return build_query_override_error_result(overriding_properties)
        return encode_query_string(query_pairs)

    def _query_pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        """Return the pairs ``value`` is sent as, in the style and explode of the spec parameter ``name``."""
        parameter = self._query_parameters.get(name)
        if parameter is None:
            # No parameter declares the key, so it takes ``form``'s default, which explodes.
            return self._UNDECLARED_QUERY_SERIALIZER.pairs(name, value, explode=True)
        return self._QUERY_SERIALIZERS_BY_STYLE[parameter.style].pairs(name, value, explode=parameter.explode)


def _spec_names_by_either_name(parameters: list[ParameterInfo]) -> dict[str, str]:
    """Map each parameter's spec name and sanitised name to its spec name."""
    return {key: parameter.name for parameter in parameters for key in (sanitize_name(parameter.name), parameter.name)}
