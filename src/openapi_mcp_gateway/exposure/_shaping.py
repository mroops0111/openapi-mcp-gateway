import logging
import re

from ..openapi import OperationInfo, ParameterInfo, ParamOverride
from ._shared import _get_override


logger = logging.getLogger(__name__)


def _declared_parameter(name: str, param_override: ParamOverride) -> ParameterInfo:
    """Build a friendly :class:`ParameterInfo` from a declaring ``params`` entry.

    The location is a placeholder, since a declared parameter reaches the upstream through the
    ``request`` JSONata expression rather than the spec-driven location assembly.
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


def shape_operation(operation: OperationInfo) -> OperationInfo:
    """Apply ``x-mcp-integration.tool.params`` to the LLM-facing surface of ``operation``.

    Returns a shaped copy. The input operation and its parameter schemas are never mutated.
    Only what the LLM sees is changed here.
    The value actually sent upstream is shaped later, by the ``request`` JSONata expression.

    ``tool.params_strategy`` chooses how ``params`` relates to the spec's own parameters.
    With ``merge`` each entry tweaks the matching spec parameter and the undeclared ones stay visible,
    so naming a parameter the spec does not define is rejected at build time.
    With ``replace`` the declared entries are the entire surface and every spec parameter is dropped,
    so a ``request`` expression is required to route the declared parameters to the upstream.
    """
    tool_override = _get_override(operation, 'tool')
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
        shaped_operation.parameters = [_declared_parameter(name, param) for name, param in params.items()]
        return shaped_operation

    unknown_names = [name for name in params if name not in spec_names]
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
        _apply_tweak(parameter, param_override)
        if param_override.hidden:
            # A hidden parameter is kept only if it has a default to inject upstream,
            # staying out of the schema but still filling its slot. Otherwise drop it.
            if parameter.send_default:
                parameter.visible = False
                kept_parameters.append(parameter)
            continue
        kept_parameters.append(parameter)

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
