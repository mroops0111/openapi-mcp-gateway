import json
import re
import typing
import urllib.parse

import pydantic
from fastapi.encoders import jsonable_encoder
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .sampling import SchemaSampler


HTTP_METHODS = ('get', 'put', 'post', 'delete', 'patch', 'head', 'options')

# What a request no operation of the spec matches gets back, so a model sees a plain upstream failure.
NOT_FOUND_BODY = {'message': 'Not Found'}

PATH_PARAMETER = re.compile(r'\{[^}/]+\}')

# Where the replay took a response from, the task's own data, the spec's sample, or nowhere for a 404.
Answer = typing.Literal['task', 'spec', 'not_found']


class RecordedRequest(pydantic.BaseModel):
    """One request the gateway sent upstream, as the replay server received it."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    server: str
    method: str
    path: str
    query: dict[str, list[str]]
    body: typing.Any = None
    # The spec operation the request matched, as ``METHOD /template``, or None when it matched none.
    operation: str | None
    # A read the spec answered got a sample the task never set, which tells why a model lost its way after it.
    answer: Answer = 'spec'

    def describe(self) -> str:
        return f'{self.method} {self.path} on {self.server}'


class ResponseOverride(pydantic.BaseModel):
    """A response a task sets for one request, where the spec's own sample would not carry what the task needs.

    ``path`` is a concrete path, such as ``/repos/acme/api/pulls/42``, or a spec template with placeholders.
    ``query`` names values the request's query must carry, so one search endpoint can answer each kind it is asked for.
    When several match, the one naming the most query values answers, and lists that tie are merged.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    server: str
    method: str
    path: str
    query: dict[str, str] = pydantic.Field(default_factory=dict)
    status: int = 200
    body: typing.Any = None


class _OperationRoute(pydantic.BaseModel):
    method: str
    template: str
    pattern: re.Pattern[str]
    status: int
    body: typing.Any


class ReplayUpstream:
    """Stands in for every upstream API of a run, answering each request from the spec and recording it.

    A request a task overrides gets the override.
    Any other request that matches an operation of the spec gets that operation's documented example,
    or a fixed sample built from its response schema,
    so a model can call any operation, not only the ones a task expected.
    A request that matches no operation gets a 404.
    The gateway reaches server ``name`` at ``<base URL>/<name>``.
    """

    def __init__(self, documents: typing.Mapping[str, typing.Mapping[str, typing.Any]]):
        self._routes = {server: _routes_of(document) for server, document in documents.items()}
        self._overrides: list[ResponseOverride] = []
        self.requests: list[RecordedRequest] = []
        self.app = Starlette(
            routes=[Route('/{server}/{path:path}', self._handle, methods=list(map(str.upper, HTTP_METHODS)))]
        )

    def start_task(self, overrides: typing.Sequence[ResponseOverride]) -> None:
        """Clear the recorded requests and set the overrides one task's run uses."""
        self._overrides = list(overrides)
        self.requests = []

    async def _handle(self, request: Request) -> Response:
        server = request.path_params['server']
        path = '/' + request.path_params['path']
        method = request.method.lower()
        route = _match(self._routes.get(server, []), method, path)
        query = urllib.parse.parse_qs(request.url.query, keep_blank_values=True)
        matching = [candidate for candidate in self._overrides if _overrides(candidate, server, method, path, query)]
        override = _most_specific(matching)
        answer: Answer = 'task' if override is not None else 'spec' if route is not None else 'not_found'
        self.requests.append(
            RecordedRequest(
                server=server,
                method=method.upper(),
                path=path,
                query=query,
                body=_parse_body(await request.body(), request.headers.get('content-type', '')),
                operation=f'{route.method.upper()} {route.template}' if route else None,
                answer=answer,
            )
        )
        if override is not None:
            return _json_response(override.status, override.body)
        if route is None:
            return JSONResponse(NOT_FOUND_BODY, status_code=404)
        return _json_response(route.status, route.body)


def _routes_of(document: typing.Mapping[str, typing.Any]) -> list[_OperationRoute]:
    sampler = SchemaSampler(document)
    routes = []
    for template, path_item in (document.get('paths') or {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            status, body = sampler.response_body(operation)
            routes.append(
                _OperationRoute(
                    method=method, template=template, pattern=_template_pattern(template), status=status, body=body
                )
            )
    # A literal segment beats a placeholder, so /pulls/comments wins over /pulls/{pull_number}.
    routes.sort(key=lambda route: (len(PATH_PARAMETER.findall(route.template)), -len(route.template)))
    return routes


def _template_pattern(template: str) -> re.Pattern[str]:
    parts = PATH_PARAMETER.split(template)
    return re.compile('^' + '[^/]+'.join(re.escape(part) for part in parts) + '/?$')


def _match(routes: typing.Sequence[_OperationRoute], method: str, path: str) -> _OperationRoute | None:
    return next((route for route in routes if route.method == method and route.pattern.match(path)), None)


def _overrides(
    override: ResponseOverride, server: str, method: str, path: str, query: typing.Mapping[str, list[str]]
) -> bool:
    if override.server != server or override.method.lower() != method:
        return False
    if any(value not in query.get(name, []) for name, value in override.query.items()):
        return False
    return override.path == path or bool(_template_pattern(override.path).match(path))


def _most_specific(overrides: typing.Sequence[ResponseOverride]) -> ResponseOverride | None:
    """The override naming the most query values, or several such overrides merged.

    A request asking for two kinds at once, such as two statuses, fits one override for each.
    When each answers a list, the request gets every item of them, else the most general override answers.
    """
    if not overrides:
        return None
    size = max(len(override.query) for override in overrides)
    fitting = [override for override in overrides if len(override.query) == size]
    if len(fitting) == 1:
        return fitting[0]
    if all(isinstance(override.body, list) for override in fitting):
        items = [item for override in fitting for item in override.body]
        return fitting[0].model_copy(
            update={'body': [item for index, item in enumerate(items) if item not in items[:index]]}
        )
    return min(overrides, key=lambda override: len(override.query))


def _parse_body(raw: bytes, content_type: str) -> typing.Any:
    if not raw:
        return None
    text = raw.decode('utf-8', errors='replace')
    if 'json' in content_type:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if 'x-www-form-urlencoded' in content_type:
        return urllib.parse.parse_qs(text, keep_blank_values=True)
    return text


def _json_response(status: int, body: typing.Any) -> Response:
    """Answer with ``body`` as JSON, writing a date as text, since a YAML spec's examples load as dates."""
    if body is None:
        return Response(status_code=status)
    return JSONResponse(jsonable_encoder(body), status_code=status)
