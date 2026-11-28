import contextlib
import pathlib
import typing

import fastapi
import pydantic
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.capabilities import ToolSearch
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

from ..spec_pins import without_security
from .dataset import Benchmark, ModelSpec, Scenario, Task
from .grading import Grade, grade, reads
from .local_server import serve
from .replay import RecordedRequest, ReplayUpstream


# Errors that say nothing about the variant or the model's skill, which the evaluation retries instead of scoring.
TRANSIENT_ERRORS = (ModelRateLimitError, ModelUnavailableError, ModelConnectionError, ModelServerError)

# The server fields that override a spec's security, left out since every spec is served without it.
_SECURITY_OVERRIDE_FIELDS = ('security_schemes', 'security')

# The outcomes of a tool call that the model has to recover from, such as arguments the gateway refused.
FAILED_TOOL_OUTCOMES = frozenset({'failed', 'retried'})


class RunOutcome(pydantic.BaseModel):
    """What one run of a task did, with its grade and what it cost."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    grade: Grade
    requests: list[RecordedRequest]
    input_tokens: int
    # The part of input_tokens read from or written to the provider's prompt cache.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int
    # What the run's model requests cost in US dollars, priced one by one, or None for a model with no known price.
    cost: float | None = None
    model_requests: int
    tool_calls: int
    tool_errors: int
    # Reads the spec's sample answered, data the task never set, which can mislead a model on a correct route.
    unset_reads: list[str] = pydantic.Field(default_factory=list)
    # True when the provider failed the run, such as by a rate limit, which says nothing about the variant or model.
    errored: bool = False
    # Why the run stopped before the model finished, such as a provider refusing that many tools, or None.
    failure: str | None = None


class ScenarioRun:
    """One scenario served for a run, with the replay upstream and a gateway per variant, both on loopback ports.

    A model reaches each server over HTTP, as a real client would, through Pydantic AI's MCP toolset.
    The gateway loads each spec without its ``security``, since the replay checks no credential.
    """

    def __init__(self, benchmark: Benchmark, scenario: Scenario, spec_paths: typing.Mapping[str, str]):
        self.benchmark = benchmark
        self.scenario = scenario
        self._spec_paths = {spec: str(without_security(pathlib.Path(path))) for spec, path in spec_paths.items()}
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
                    **_without_security_overrides(self.scenario.variants[variant].server_fragment(server)),
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

    def agent(
        self, model: Model | str, model_spec: ModelSpec | None, variant: str, mcp_urls: typing.Mapping[str, str]
    ) -> Agent:
        """An agent for ``model`` with one MCP toolset per server, told only what the manifest's system prompt says.

        Under a variant with ``tool_search``, every tool is deferred until Pydantic AI's tool search finds it,
        with the search the provider offers natively, else Pydantic AI's keyword search.
        """
        tool_search = self.scenario.variants[variant].tool_search
        toolsets = [self._toolset(server, url) for server, url in mcp_urls.items()]
        return Agent(
            model,
            system_prompt=self.benchmark.manifest.system_prompt,
            toolsets=[toolset.defer_loading() for toolset in toolsets] if tool_search else toolsets,
            capabilities=[ToolSearch()] if tool_search else [],
            model_settings=typing.cast(typing.Any, model_spec.settings) if model_spec else None,
        )

    def tool_prefix(self, server: str) -> str:
        """What a server's tool names start with, nothing for one server and its name when several share a run.

        Two servers can both have a tool of one name, such as ``call_operation``,
        so a run with several tells them apart.
        """
        return f'{server}_' if len(self.scenario.servers) > 1 else ''

    def _toolset(self, server: str, url: str) -> typing.Any:
        # A server's instructions reach the model, as Claude Desktop and Claude Code place them in its context.
        toolset = MCPToolset(
            url, id=server, max_retries=self.benchmark.manifest.tool_retries, include_instructions=True
        )
        prefix = self.tool_prefix(server)
        return toolset.prefixed(prefix.removesuffix('_')) if prefix else toolset

    async def run_task(self, agent: Agent, task: Task) -> RunOutcome:
        """Run ``task`` once with ``agent`` and grade what reached the upstream.

        A provider failure, such as a rate limit, marks the outcome errored rather than raising,
        so what the attempt cost is still counted.
        """
        self.replay.start_task(task.upstream_responses)
        failure = None
        errored = False
        with capture_run_messages() as messages:
            try:
                await agent.run(
                    task.prompt, usage_limits=UsageLimits(request_limit=self.benchmark.manifest.request_limit)
                )
            except TRANSIENT_ERRORS as error:
                failure = f'{type(error).__name__}: {error}'
                errored = True
            except AgentRunError as error:
                failure = f'{type(error).__name__}: {error}'
        requests = list(self.replay.requests)
        read_only_operations = self.read_only_operations()
        unset_reads = {
            request.describe()
            for request in requests
            if request.answer == 'spec'
            and reads(request.method, request.path, read_only_operations.get(request.server, ()))
        }
        return RunOutcome(
            grade=grade(task.expected_calls, task.forbidden_calls, requests, read_only_operations),
            requests=requests,
            unset_reads=sorted(unset_reads),
            errored=errored,
            failure=failure,
            **_costs(messages),
        )

    def read_only_operations(self) -> dict[str, list[str]]:
        """The manifest's read-only operations for each server of the scenario, by the server's name."""
        listed = self.benchmark.manifest.read_only_operations
        return {server: listed.get(spec, []) for server, spec in self.scenario.servers.items()}


def _costs(messages: typing.Sequence[ModelMessage]) -> dict[str, typing.Any]:
    responses = [message for message in messages if isinstance(message, ModelResponse)]
    parts = [part for message in messages for part in message.parts]
    return {
        'cost': _price(responses),
        'input_tokens': sum(response.usage.input_tokens for response in responses),
        'cache_read_tokens': sum(response.usage.cache_read_tokens for response in responses),
        'cache_write_tokens': sum(response.usage.cache_write_tokens for response in responses),
        'output_tokens': sum(response.usage.output_tokens for response in responses),
        'model_requests': len(responses),
        'tool_calls': sum(isinstance(part, ToolCallPart) for part in parts),
        'tool_errors': sum(isinstance(part, ToolReturnPart) and part.outcome in FAILED_TOOL_OUTCOMES for part in parts),
    }


def _price(responses: typing.Sequence[ModelResponse]) -> float | None:
    """What ``responses`` cost, each priced on its own, since a long request can cost more per token.

    None when one has no known price, or reports too little usage to price, as Pydantic AI's own best effort treats it.
    """
    if any(response.model_name is None for response in responses):
        return None
    try:
        return float(sum(response.cost().total_price for response in responses))
    except (LookupError, ValueError):
        return None


def _without_security_overrides(fragment: typing.Mapping[str, typing.Any]) -> dict[str, typing.Any]:
    """``fragment`` without the security it would override, at the root and on each operation.

    The specs are served without ``security``, so an override would name schemes they no longer declare,
    or ask for credentials no replay upstream checks.
    """
    kept = {key: value for key, value in fragment.items() if key not in _SECURITY_OVERRIDE_FIELDS}
    if isinstance(operations := kept.get('operations'), dict):
        kept['operations'] = {
            operation_id: {key: value for key, value in override.items() if key != 'security'}
            if isinstance(override, dict)
            else override
            for operation_id, override in operations.items()
        }
    return kept
