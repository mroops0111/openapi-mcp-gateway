import typing

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from .dataset import SolutionCall, Task


# The meta-tool that calls an operation by name under dynamic exposure.
CALL_OPERATION_TOOL = 'call_operation'


class SolutionToolMissingError(LookupError):
    """A variant exposes neither the operation a solution calls nor a meta-tool that reaches it."""


def solution_model(task: Task, tool_prefixes: typing.Mapping[str, str]) -> FunctionModel:
    """A model that makes ``task``'s solution calls in one turn and then stops, needing no provider.

    It calls an operation's own tool where the variant exposes one, and ``call_operation`` where the variant is dynamic,
    so the same solution proves a task passes under static and dynamic exposure alike.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answered = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if answered:
            return ModelResponse(parts=[TextPart('Done.')])
        tool_names = {tool.name for tool in info.function_tools}
        return ModelResponse(
            parts=[_tool_call(call, tool_prefixes.get(call.server, ''), tool_names) for call in task.solution]
        )

    return FunctionModel(respond)


def _tool_call(call: SolutionCall, prefix: str, tool_names: set[str]) -> ToolCallPart:
    if prefix + call.operation in tool_names:
        return ToolCallPart(tool_name=prefix + call.operation, args=call.arguments)
    if prefix + CALL_OPERATION_TOOL in tool_names:
        return ToolCallPart(
            tool_name=prefix + CALL_OPERATION_TOOL, args={'name': call.operation, 'arguments': call.arguments}
        )
    raise SolutionToolMissingError(f'No tool reaches {call.operation} on {call.server}')
