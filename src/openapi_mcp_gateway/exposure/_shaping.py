import decimal
import logging
import re
import typing

import pydantic
from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from mcp.types import CallToolResult

from ..openapi import OperationInfo, ParameterInfo, ParamOverride
from ._media import MediaPromoter
from ._shared import get_override, sanitize_name, split_by_location
from ._tool_result import (
    build_binary_result,
    build_success_result,
    build_transform_error_result,
    build_validation_error_result,
)
from ._transform import TransformError, apply_transform, compile_expression
from ._upstream_http import UpstreamBinaryBody
from ._upstream_request import UpstreamArguments


logger = logging.getLogger(__name__)


def shape_operation(operation: OperationInfo) -> OperationInfo:
    """Apply ``x-mcp-integration.tool.params`` to the LLM-facing surface of ``operation``.

    Returns a shaped copy.
    The input operation and its parameter schemas are never mutated.
    Only what the LLM sees is changed here.
    The value actually sent upstream is shaped later, by the ``request`` JSONata expression.

    ``tool.params_strategy`` chooses how ``params`` relates to the spec's own parameters.
    With ``merge`` each entry tweaks the matching spec parameter and the undeclared ones stay visible,
    so naming a parameter the spec does not define is rejected at build time.
    With ``replace`` the declared entries are the entire surface and every spec parameter is dropped,
    so a ``request`` expression is required to route the declared parameters to the upstream.
    The one exception is an entry that names a spec header, which is tweaked as under ``merge`` and stays a header,
    so a required header such as an API version survives a replaced surface.
    """
    tool_override = get_override(operation, 'tool')
    if tool_override is None or not tool_override.params:
        return operation

    params = tool_override.params
    if tool_override.params_strategy is None:
        raise ValueError(
            f'Operation "{operation.operation_id}" sets tool.params but no tool.params_strategy. '
            'Set params_strategy to "merge" (layer onto the spec) or "replace" (declare the whole surface).'
        )

    shaped_operation = operation.model_copy(deep=True)
    spec_names = {parameter.name for parameter in operation.parameters}

    if tool_override.params_strategy == 'replace':
        if not tool_override.request:
            raise ValueError(
                f'Operation "{operation.operation_id}" uses params_strategy "replace" but no tool.request. '
                'Replaced parameters reach the upstream only through a request expression.'
            )
        spec_headers = {
            parameter.name: parameter for parameter in shaped_operation.parameters if parameter.location == 'header'
        }
        declared_parameters: list[ParameterInfo] = []
        for name, param_override in params.items():
            spec_header = spec_headers.get(name)
            if spec_header is None:
                declared_parameters.append(_declared_parameter(name, param_override))
            elif (header := _tweak_spec_parameter(spec_header, param_override)) is not None:
                declared_parameters.append(header)
        shaped_operation.parameters = declared_parameters
        return shaped_operation

    unsendable_parameter_names = [name for name in params if name in operation.unsendable_query_parameters]
    if unsendable_parameter_names:
        logger.warning(
            'Operation "%s" tweaks query parameter(s) %s the gateway left out, since it cannot send their style. '
            'The tweak is skipped.',
            operation.operation_id,
            unsendable_parameter_names,
        )
    unknown_names = [
        name for name in params if name not in spec_names and name not in operation.unsendable_query_parameters
    ]
    if unknown_names:
        raise ValueError(
            f'Operation "{operation.operation_id}" uses params_strategy "merge" but names parameter(s) '
            f'{unknown_names} the spec does not define. Merge only tweaks existing parameters. '
            'Use params_strategy "replace" to declare a new friendly surface.'
        )

    kept_parameters: list[ParameterInfo] = []
    for parameter in shaped_operation.parameters:
        param_override = params.get(parameter.name)
        if param_override is None:
            kept_parameters.append(parameter)
            continue
        if (tweaked := _tweak_spec_parameter(parameter, param_override)) is not None:
            kept_parameters.append(tweaked)

    if not tool_override.request:
        kept_path_parameter_names = {parameter.name for parameter in kept_parameters if parameter.location == 'path'}
        unfilled_path_placeholders = [
            placeholder_name
            for placeholder_name in re.findall(r'{(\w+)}', operation.path)
            if placeholder_name not in kept_path_parameter_names
        ]
        if unfilled_path_placeholders:
            raise ValueError(
                f'Operation "{operation.operation_id}" has path parameter(s) {unfilled_path_placeholders} '
                'that shaping left with nothing to fill them, '
                'no visible parameter, no default, and no tool.request. '
                'Give each a default, keep it visible, or add a tool.request that supplies it.'
            )

    shaped_operation.parameters = kept_parameters
    return shaped_operation


class RequestShaper:
    """Turn a tool call's arguments into upstream arguments, by the shaped operation the LLM sees.

    The arguments are checked against ``input_schema``, the schema the tool advertises, when one is given.
    Each default the LLM omitted is added, and a ``request`` JSONata expression in the ``tool`` override,
    when set, builds the upstream arguments whole.
    Otherwise each argument passes through for its parameter.
    The expression is compiled here, so a syntax error surfaces at startup.
    """

    def __init__(self, operation: OperationInfo, input_schema: dict[str, typing.Any] | None = None) -> None:
        path_parameters, query_parameters, header_parameters, body_parameters = split_by_location(operation.parameters)
        self._path_parameters = _parameters_keyed_by_sanitised_name(path_parameters)
        self._query_parameters = _parameters_keyed_by_sanitised_name(query_parameters)
        self._header_parameters = _parameters_keyed_by_sanitised_name(header_parameters)
        self._body_parameters = _parameters_keyed_by_sanitised_name(body_parameters)
        self._injected_defaults = {
            sanitize_name(parameter.name): parameter.schema_['default']
            for parameter in operation.parameters
            if parameter.send_default
        }
        tool_override = get_override(operation, 'tool')
        self._request_transform = (
            compile_expression(tool_override.request, label=f'{operation.operation_id} request')
            if tool_override and tool_override.request
            else None
        )
        self._validator = (
            _ArgumentValidator(input_schema, format_checker=FormatChecker()) if input_schema is not None else None
        )

    def shape_request(self, arguments: dict[str, typing.Any]) -> UpstreamArguments | CallToolResult:
        """Return the upstream arguments, or the error result of invalid arguments or a failed ``request``."""
        provided = _to_jsonable({name: value for name, value in arguments.items() if value is not None})
        if self._validator is not None:
            errors = sorted(self._validator.iter_errors(provided), key=lambda error: list(error.path))
            if errors:
                return build_validation_error_result(errors)
        # A default is sent upstream only when the LLM omitted it.
        provided = {**_to_jsonable(self._injected_defaults), **provided}
        if self._request_transform is None:
            return UpstreamArguments(
                path=_arguments_by_spec_name(provided, self._path_parameters),
                query=_arguments_by_spec_name(provided, self._query_parameters),
                headers=_arguments_by_spec_name(provided, self._header_parameters),
                body=_arguments_by_spec_name(provided, self._body_parameters),
            )
        try:
            upstream_object = apply_transform(self._request_transform, provided)
        except TransformError as error:
            return build_transform_error_result('request', error)
        return UpstreamArguments(unlocated=upstream_object or {})


class ResponseShaper:
    """Turn an upstream success body into the tool result, by the shaped operation the LLM sees.

    Media is promoted out of the body first, from the response schema and the ``tool`` override's ``media``,
    and a ``response`` JSONata expression then reshapes what is left.
    The expression is compiled here, so a syntax error surfaces at startup.
    """

    def __init__(self, operation: OperationInfo) -> None:
        tool_override = get_override(operation, 'tool')
        self._response_transform = (
            compile_expression(tool_override.response, label=f'{operation.operation_id} response')
            if tool_override and tool_override.response
            else None
        )
        # The override wins per path, so an author can correct or add to what the spec declares.
        self._media_promoter = MediaPromoter(operation.response_media, tool_override.media if tool_override else {})

    def shape_response(self, body: typing.Any, url: str) -> CallToolResult:
        """Return the tool result of ``body``, the upstream response at ``url``."""
        # A response transform reshapes JSON, so it has nothing to apply to in a binary body.
        if isinstance(body, UpstreamBinaryBody):
            return build_binary_result(body)
        # Media paths name fields of the upstream response, so they are promoted before a transform reshapes it.
        body, media_contents = self._media_promoter.promote(body, url)
        if self._response_transform is not None:
            try:
                body = apply_transform(self._response_transform, body)
            except TransformError as error:
                return build_transform_error_result('response', error)
        return build_success_result(body, media_contents)


def _declared_parameter(name: str, param_override: ParamOverride) -> ParameterInfo:
    """Build a friendly :class:`ParameterInfo` from a declaring ``params`` entry.

    The location is a placeholder.
    A declared parameter reaches the upstream through the ``request`` JSONata expression,
    which bypasses the spec-driven location assembly.
    A declared ``default`` is marked to be sent upstream when the LLM omits the parameter.
    """
    fragment = param_override.schema_fragment
    return ParameterInfo(
        name=name,
        location='query',
        required=param_override.required,
        description=fragment.get('description', ''),
        schema=fragment,
        send_default='default' in fragment,
    )


def _apply_tweak(parameter: ParameterInfo, param_override: ParamOverride) -> None:
    """Apply one ``params`` entry to a matching spec ``parameter`` in place.

    A typed entry replaces the parameter's schema wholesale.
    An untyped entry adjusts the ``default`` (also sending it on omit) and the ``description``.
    """
    fragment = param_override.schema_fragment
    if param_override.declares_schema:
        parameter.schema_ = fragment
        parameter.required = param_override.required
        parameter.send_default = 'default' in fragment
    elif 'default' in fragment:
        parameter.schema_ = {**parameter.schema_, 'default': fragment['default']}
        parameter.required = False
        parameter.send_default = True
    if 'description' in fragment:
        parameter.description = fragment['description']


def _tweak_spec_parameter(parameter: ParameterInfo, param_override: ParamOverride) -> ParameterInfo | None:
    """Apply one ``params`` entry to a spec ``parameter`` in place, returning it, or ``None`` to drop it.

    A hidden parameter is kept only if it has a default to inject upstream,
    staying out of the schema but still filling its slot.
    Otherwise it is dropped.
    """
    _apply_tweak(parameter, param_override)
    if not param_override.hidden:
        return parameter
    if not parameter.send_default:
        return None
    parameter.visible = False
    return parameter


def _parameters_keyed_by_sanitised_name(parameters: list[ParameterInfo]) -> dict[str, ParameterInfo]:
    """Build a lookup from sanitised parameter name to the original :class:`ParameterInfo`."""
    return {sanitize_name(parameter.name): parameter for parameter in parameters}


def _arguments_by_spec_name(
    arguments: dict[str, typing.Any], parameters_by_sanitised_name: dict[str, ParameterInfo]
) -> dict[str, typing.Any]:
    """Pick the ``arguments`` that ``parameters_by_sanitised_name`` declares, keyed by the original API name."""
    return {
        parameter.name: arguments[sanitised_name]
        for sanitised_name, parameter in parameters_by_sanitised_name.items()
        if sanitised_name in arguments
    }


def _to_jsonable(value: typing.Any) -> typing.Any:
    """Recursively convert pydantic models into plain JSON-serialisable structures.

    ``schema_to_python_type`` turns an object-shaped parameter into a dynamic pydantic model,
    so the value reaching here may be a model, a list of models, or a dict whose values are models.
    Validation, JSONata and the upstream body all need plain JSON, so each model is dumped first.
    ``exclude_none`` leaves an omitted optional field absent rather than null.
    """
    if isinstance(value, pydantic.BaseModel):
        return value.model_dump(mode='json', exclude_none=True)
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _decimal_multiple_of(
    validator: typing.Any, divisor: typing.Any, instance: typing.Any, schema: dict[str, typing.Any]
) -> typing.Iterator[ValidationError]:
    """Check ``multipleOf`` on the decimal values a model writes, not on their binary floats.

    A float cannot hold 41.8826 or 0.0001 exactly, so dividing them leaves a remainder,
    and the stock check refuses a coordinate written to four places against ``multipleOf: 0.0001``.
    Each number is read back from its shortest decimal form, which is the one the model wrote.
    A quotient too large for decimal arithmetic falls back to the stock check.
    """
    if not validator.is_type(instance, 'number'):
        return
    try:
        remainder = decimal.Decimal(repr(instance)) % decimal.Decimal(repr(divisor))
    except decimal.InvalidOperation:
        yield from Draft202012Validator.VALIDATORS['multipleOf'](validator, divisor, instance, schema)
        return
    if remainder != 0:
        yield ValidationError(f'{instance!r} is not a multiple of {divisor!r}')


# The tool argument validator, with ``multipleOf`` checked in decimal.
# It follows the function it uses, since Python builds it at import time.
_ArgumentValidator = validators.extend(Draft202012Validator, {'multipleOf': _decimal_multiple_of})
