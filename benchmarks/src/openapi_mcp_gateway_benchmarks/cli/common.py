import pathlib
import typing

import click
import httpx
import pydantic

from ..errors import BenchmarkDataError
from ..gateway_connection import ToolCallError
from ..spec_pins import SpecChecksumError


# The directories a command reads a benchmark from and writes its working files into.
BENCHMARK_DIRECTORY = click.Path(exists=True, file_okay=False, path_type=pathlib.Path)
WORK_DIRECTORY = click.Path(file_okay=False, path_type=pathlib.Path)

# The errors a command reports in one line, since each names what to fix, where any other error is a bug to trace.
_HANDLED_ERRORS = (BenchmarkDataError, SpecChecksumError, ToolCallError, OSError, httpx.HTTPError)


class HandledErrorsGroup(click.Group):
    """A command group that reports bad data, a stale pin, an unreachable spec, or a missing file in one line."""

    def invoke(self, ctx: click.Context) -> typing.Any:
        try:
            return super().invoke(ctx)
        except _HANDLED_ERRORS as error:
            raise click.ClickException(str(error)) from error


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


def emit_report(report: pydantic.BaseModel, markdown: str, output: pathlib.Path | None) -> None:
    """Print ``markdown``, then write ``report`` to ``output`` as JSON, so a failed write never loses the tables."""
    click.echo(markdown, nl=False)
    if output is not None:
        output.write_text(report.model_dump_json(indent=2) + '\n', encoding='utf-8')


def _check_output_directory(
    ctx: click.Context, parameter: click.Parameter, output: pathlib.Path | None
) -> pathlib.Path | None:
    if output is not None and not output.parent.is_dir():
        raise click.BadParameter(f'{output.parent} is not a directory', ctx=ctx, param=parameter)
    return output
