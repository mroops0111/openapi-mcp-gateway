import contextlib
import typing

import fastapi
import pydantic
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelServerError,
    ModelUnavailableError,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import load_spec
from openapi_mcp_gateway.settings import GatewayConfig, ServerConfig

from ..local_server import serve
from .dataset import Benchmark, ModelSpec, Scenario, Task
from .grading import Grade, grade
from .replay import RecordedRequest, ReplayUpstream


# Errors that say nothing about the variant or the model's skill, which the evaluation retries instead of scoring.
TRANSIENT_ERRORS = (ModelRateLimitError, ModelUnavailableError, ModelConnectionError, ModelServerError)

# The outcomes of a tool call that the model has to recover from, such as arguments the gateway refused.
FAILED_TOOL_OUTCOMES = frozenset({'failed', 'retried'})


class RunOutcome(pydantic.BaseModel):
    """What one run of a task did, with its grade and what it cost."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    grade: Grade
    requests: list[RecordedRequest]
    input_tokens: int
    output_tokens: int
    model_requests: int
    tool_calls: int
    tool_errors: int
    # Why the run stopped before the model finished, such as a provider refusing that many tools, or None.
    failure: str | None = None


class ScenarioRun:
    """One scenario served for a run, with the replay upstream and a gateway per variant, both on loopback ports.

    A model reaches each server over HTTP, as a real client would, through Pydantic AI's MCP toolset.
    """

    def __init__(self, benchmark: Benchmark, scenario: Scenario, spec_paths: typing.Mapping[str, str]):
        self.benchmark = benchmark
        self.scenario = scenario
        self._spec_paths = dict(spec_paths)
        self.replay = ReplayUpstream({server: load_spec(spec_paths[spec]) for server, spec in scenario.servers.items()})
        self._replay_url = ''

    @contextlib.asynccontextmanager
    async def serving(self) -> typing.AsyncIterator[typing.Self]:
        async with serve(self.replay.app) as replay_url:
            self._replay_url = replay_url
            yield self

    def gateway_config(self, variant: str) -> GatewayConfig:
        """The gateway config that serves every server of the scenario under ``variant``, upstream on the replay."""
        servers = [
            ServerConfig.model_validate(
                {
                    **self.scenario.variants[variant].server_fragment(server),
                    'name': server,
                    'spec': self._spec_paths[spec],
                    'base_url': f'{self._replay_url}/{server}',
                }
            )
            for server, spec in self.scenario.servers.items()
        ]
        return GatewayConfig(servers=servers)

    @contextlib.asynccontextmanager
    async def gateway(self, variant: str) -> typing.AsyncIterator[dict[str, str]]:
        """Serve the scenario's gateway under ``variant``, yielding each server's MCP URL."""
        gateway = Gateway.from_config(self.gateway_config(variant))

        @contextlib.asynccontextmanager
        async def lifespan(_app: fastapi.FastAPI) -> typing.AsyncIterator[None]:
            async with gateway.lifespan():
                yield

        app = fastapi.FastAPI(lifespan=lifespan)
        gateway.mount(app)
        async with serve(app) as gateway_url:
            yield {server: f'{gateway_url}/{server}/mcp' for server in self.scenario.servers}

    def agent(self, model: Model | str, model_spec: ModelSpec | None, mcp_urls: typing.Mapping[str, str]) -> Agent:
        """An agent for ``model`` with one MCP toolset per server, told only what the manifest's system prompt says."""
        return Agent(
            model,
            system_prompt=self.benchmark.manifest.system_prompt,
            toolsets=[self._toolset(server, url) for server, url in mcp_urls.items()],
            model_settings=typing.cast(typing.Any, model_spec.settings) if model_spec else None,
        )

    def tool_prefix(self, server: str) -> str:
        """What a server's tool names start with, nothing for one server and its name when several share a run.

        Two servers can both have a tool of one name, such as ``call_operation``,
        so a run with several tells them apart.
        """
        return f'{server}_' if len(self.scenario.servers) > 1 else ''

    def _toolset(self, server: str, url: str) -> typing.Any:
        toolset = MCPToolset(url, id=server, max_retries=self.benchmark.manifest.tool_retries)
        prefix = self.tool_prefix(server)
        return toolset.prefixed(prefix.removesuffix('_')) if prefix else toolset

    async def run_task(self, agent: Agent, task: Task) -> RunOutcome:
        """Run ``task`` once with ``agent`` and grade what reached the upstream."""
        self.replay.start_task(task.responses)
        failure = None
        with capture_run_messages() as messages:
            try:
                await agent.run(
                    task.prompt, usage_limits=UsageLimits(request_limit=self.benchmark.manifest.request_limit)
                )
            except TRANSIENT_ERRORS:
                raise
            except AgentRunError as error:
                failure = f'{type(error).__name__}: {error}'
        requests = list(self.replay.requests)
        return RunOutcome(
            grade=grade(task.expected_calls, task.forbidden_calls, requests, self.read_only_operations()),
            requests=requests,
            failure=failure,
            **_costs(messages),
        )

    def read_only_operations(self) -> dict[str, list[str]]:
        """The manifest's read-only operations for each server of the scenario, by the server's name."""
        listed = self.benchmark.manifest.read_only_operations
        return {server: listed.get(spec, []) for server, spec in self.scenario.servers.items()}


def _costs(messages: typing.Sequence[ModelMessage]) -> dict[str, int]:
    responses = [message for message in messages if isinstance(message, ModelResponse)]
    parts = [part for message in messages for part in message.parts]
    return {
        'input_tokens': sum(response.usage.input_tokens for response in responses),
        'output_tokens': sum(response.usage.output_tokens for response in responses),
        'model_requests': len(responses),
        'tool_calls': sum(isinstance(part, ToolCallPart) for part in parts),
        'tool_errors': sum(isinstance(part, ToolReturnPart) and part.outcome in FAILED_TOOL_OUTCOMES for part in parts),
    }
