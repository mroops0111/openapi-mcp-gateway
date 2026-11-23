import logging

import click

from . import search_ranking, tool_calling, user_configs
from .common import HandledErrorsGroup


@click.group('eval', cls=HandledErrorsGroup)
@click.option('-v', '--verbose', is_flag=True, help='Log progress as it runs.')
def eval_command(verbose: bool) -> None:
    """Measure what your own config does for a model, on tasks you write for your API.

    \b
        omgw eval check --task-file tasks.yaml --config config.yml
        omgw eval run --task-file tasks.yaml --config config.yml --model anthropic:claude-haiku-5-5
    """
    _configure_logging(verbose)


@click.group('benchmark', cls=HandledErrorsGroup, hidden=True)
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and variant as it runs.')
def benchmark_command(verbose: bool) -> None:
    """Run, check, and make anew the gateway's own benchmarks, from the repository root.

    \b
        omgw benchmark tool-calling check
        omgw benchmark search-ranking check
    """
    _configure_logging(verbose)


eval_command.add_command(user_configs.run)
eval_command.add_command(user_configs.check)
benchmark_command.add_command(tool_calling.group)
benchmark_command.add_command(search_ranking.group)


def _configure_logging(verbose: bool) -> None:
    # The gateway logs every registration at INFO, which buries a command's own output.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    if verbose:
        logging.getLogger('openapi_mcp_gateway_eval').setLevel(logging.INFO)
