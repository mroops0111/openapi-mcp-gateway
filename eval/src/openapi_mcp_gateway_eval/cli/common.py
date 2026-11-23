import pathlib
import re
import sys
import tempfile
import typing

import click
import httpx
import pydantic
from pydantic_ai.exceptions import UserError

from ..errors import BenchmarkDataError
from ..gateway_connection import ToolCallError
from ..spec_pins import SpecChecksumError
from ..tool_calling.dataset import Benchmark
from ..tool_calling.reach import IntentCoverage
from ..tool_calling.user_configs import DEFAULT_CONFIG_NAME
from ..tool_calling.validation import ValidationProblem


# Every input is an --option, never a positional argument, as in omgw serve, so a command reads the same in any order.
# An option is named in the singular, since each use gives one value, and one that may repeat is collected in a plural.
# A file option says it is a file, as --task-file, unless its name already says what the file is, as --config.

# Where each benchmark's data sits, relative to the repository root a maintainer runs the commands from.
DEFAULT_BENCHMARK_DIRECTORIES = {'tool-calling': 'eval/tool_calling', 'search-ranking': 'eval/search_ranking'}

# A file the user wrote, such as a config or the tasks.
INPUT_FILE = click.Path(exists=True, dir_okay=False, path_type=pathlib.Path)

# How a config is given its name, as in ``--config search=config.yml``.
NAMED_CONFIG = re.compile(r'^(?P<name>[A-Za-z0-9_-]+)=(?P<path>.+)$')

# The errors a command reports in one line, since each names what to fix, where any other error is a bug to trace.
# Pydantic AI raises UserError for a model it cannot build, such as an unknown provider or a missing API key.
_HANDLED_ERRORS = (BenchmarkDataError, SpecChecksumError, ToolCallError, OSError, httpx.HTTPError, UserError)


class HandledErrorsGroup(click.Group):
    """A command group that reports bad data, a stale pin, an unreachable spec, or a missing file in one line."""

    def invoke(self, ctx: click.Context) -> typing.Any:
        try:
            return super().invoke(ctx)
        except _HANDLED_ERRORS as error:
            raise click.ClickException(str(error)) from error


def benchmark_directory_option(benchmark: str) -> typing.Callable[..., typing.Any]:
    """The ``--benchmark-directory`` option of ``benchmark``'s commands, defaulting to where its data sits."""
    return click.option(
        '--benchmark-directory',
        type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path),
        default=DEFAULT_BENCHMARK_DIRECTORIES[benchmark],
        show_default=True,
        help="The benchmark's data.",
    )


def work_directory_option(command: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
    """The ``--work-directory`` option a model's run keeps its raw exchanges in, a new temporary one by default."""
    return click.option(
        '--work-directory',
        type=click.Path(file_okay=False, path_type=pathlib.Path),
        callback=_work_directory,
        help='Where to keep each raw exchange with a model. The default is a new temporary directory.',
    )(command)


def task_file_option(command: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
    """The ``--task-file`` option, the user's own tasks."""
    return click.option('--task-file', type=INPUT_FILE, required=True, help='The tasks to run, in YAML.')(command)


def variant_option(command: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
    """The ``--variant`` option every run takes, collected as ``variant_names``."""
    return click.option(
        '--variant', 'variant_names', multiple=True, help='A variant to run. Repeat for several; the default is all.'
    )(command)


def output_option(command: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
    """The ``--output`` option every run takes, refused before the run when its directory does not exist."""
    return click.option(
        '--output',
        type=click.Path(dir_okay=False, path_type=pathlib.Path),
        callback=_check_output_directory,
        help='Write the full report as JSON.',
    )(command)


def config_option(command: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
    """The ``--config`` option, collected as ``configs``, each config by its name."""
    return click.option(
        '--config',
        'configs',
        multiple=True,
        required=True,
        callback=_parse_configs,
        help='A gateway config, as NAME=PATH, or PATH alone for a single config, which is then named yours. '
        'Repeat for several.',
    )(command)


def emit_report(report: pydantic.BaseModel, markdown: str, output: pathlib.Path | None) -> None:
    """Print ``markdown``, then write ``report`` to ``output`` as JSON, so a failed write never loses the tables."""
    click.echo(markdown, nl=False)
    if output is not None:
        output.write_text(report.model_dump_json(indent=2) + '\n', encoding='utf-8')


def echo_measured_coverage(benchmark: Benchmark, coverage: typing.Sequence[IntentCoverage]) -> None:
    """Print the tasks each measured variant leaves out of reach, and the reads it hides from a solution."""
    for entry in coverage:
        if entry.variant not in benchmark.measured_variants:
            continue
        if entry.unreachable_tasks:
            click.echo(
                f'{entry.scenario} under {entry.variant} reaches {entry.reachable_task_count} of {entry.task_count} '
                f'tasks, missing {", ".join(entry.unreachable_tasks)}.'
            )
        if entry.hidden_lookups:
            click.echo(
                f'{entry.scenario} under {entry.variant} hides a read the solution makes in '
                f'{", ".join(entry.hidden_lookups)}.'
            )


def refuse_problems(problems: typing.Sequence[ValidationProblem]) -> None:
    """Print each problem and exit 1, so nothing goes on with tasks that do not hold together."""
    if not problems:
        return
    for problem in problems:
        click.echo(f'{problem.scenario} {problem.task_id} under {problem.variant}: {problem.problem}', err=True)
    sys.exit(1)


def _work_directory(ctx: click.Context, parameter: click.Parameter, value: pathlib.Path | None) -> pathlib.Path:
    if value is not None:
        return value
    created = pathlib.Path(tempfile.mkdtemp(prefix='omgw-'))
    click.echo(f'Keeping the raw exchanges in {created}', err=True)
    return created


def _check_output_directory(
    ctx: click.Context, parameter: click.Parameter, output: pathlib.Path | None
) -> pathlib.Path | None:
    if output is not None and not output.parent.is_dir():
        raise click.BadParameter(f'{output.parent} is not a directory', ctx=ctx, param=parameter)
    return output


def _parse_configs(ctx: click.Context, parameter: click.Parameter, values: tuple[str, ...]) -> dict[str, pathlib.Path]:
    """Each config by its name, refusing a name given twice, a missing file, or several configs left unnamed."""
    configs: dict[str, pathlib.Path] = {}
    for value in values:
        found = NAMED_CONFIG.match(value)
        if found is None and len(values) > 1:
            raise click.BadParameter(f'Name each of several configs, as NAME={value}', ctx=ctx, param=parameter)
        name, path = (found['name'], found['path']) if found else (DEFAULT_CONFIG_NAME, value)
        if name in configs:
            raise click.BadParameter(f'Two configs are named {name}', ctx=ctx, param=parameter)
        if not pathlib.Path(path).is_file():
            raise click.BadParameter(f'{path} is not a file', ctx=ctx, param=parameter)
        configs[name] = pathlib.Path(path)
    return configs
