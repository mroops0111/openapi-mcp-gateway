import typing

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from .dataset import SolutionCall, Task


# The meta-tool that calls an operation by name under dynamic exposure.
CALL_OPERATION_TOOL = 'call_operation'


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
        calls = [_tool_call(call, tool_prefixes.get(call.server, ''), tool_names) for call in task.solution]
        if None in calls:
            # A variant that hides an operation leaves the model nothing to call, so the run ends and fails its grade.
            return ModelResponse(parts=[TextPart('No tool reaches what this task needs.')])
        return ModelResponse(parts=[call for call in calls if call is not None])

    return FunctionModel(respond)


def _tool_call(call: SolutionCall, prefix: str, tool_names: set[str]) -> ToolCallPart | None:
    if prefix + call.operation in tool_names:
        return ToolCallPart(tool_name=prefix + call.operation, args=call.arguments)
    if prefix + CALL_OPERATION_TOOL in tool_names:
        return ToolCallPart(
            tool_name=prefix + CALL_OPERATION_TOOL, args={'name': call.operation, 'arguments': call.arguments}
        )
    return None
