import logging

import click

from . import search_ranking, tool_calling
from .common import HandledErrorsGroup


@click.group(cls=HandledErrorsGroup)
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and variant as it runs.')
def main(verbose: bool) -> None:
    """Benchmarks of what the gateway does for a model."""
    # The gateway logs every registration at INFO, which buries a benchmark's own output.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    if verbose:
        logging.getLogger('openapi_mcp_gateway_benchmarks').setLevel(logging.INFO)


main.add_command(search_ranking.group)
main.add_command(tool_calling.group)
