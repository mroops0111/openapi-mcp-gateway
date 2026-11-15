import datetime
import importlib.metadata
import json
import pathlib
import typing

import pydantic
from pydantic_ai import Agent, ModelRetry

from .annotation_workflow import (
    INDEPENDENT_ASSESSOR_OUTPUT_FILE,
    QUERY_AUTHOR_OUTPUT_FILE,
    AssessedQuery,
    AuthoredQuery,
    InstructionInputs,
    independent_assessor_instructions,
    query_author_instructions,
    write_assessor_queries,
    write_operation_list,
)
from .dataset import STYLES, Manifest


# What the automated annotators keep beside their answers, so a run can be traced after the fact.
PROVENANCE_FILE = 'provenance.json'
MESSAGES_SUFFIX = '.messages.json'

# How many operation lines a search of the list returns at most, enough to compare candidates without flooding.
FIND_LIMIT = 40

# How often an annotator may be sent back to fix an answer that names an unknown operation or misses a query.
OUTPUT_RETRIES = 3

AUTOMATED_SEARCH_NOTE = (
    'The find_operations tool searches the same list for a word, which helps check names and find candidates.'
)
AUTOMATED_OUTPUT_INSTRUCTIONS = (
    'Return every query in your structured answer. '
    'Every key of relevant must be an exact name from the operation list, which find_operations helps check.'
)
AUTOMATED_INPUTS = InstructionInputs(
    operations_input='The operation list at the end of these instructions',
    queries_input='The requests at the end of these instructions',
    search_note=AUTOMATED_SEARCH_NOTE,
    output_instructions=AUTOMATED_OUTPUT_INSTRUCTIONS,
)


class AnnotatorRun(pydantic.BaseModel):
    """One annotator's run on one spec, saying which model ran, when, through which framework, and at what cost."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    spec: str
    role: typing.Literal['query author', 'independent assessor']
    model: str
    finished_at: str
    pydantic_ai_version: str
    input_tokens: int
    output_tokens: int
    model_requests: int


async def annotate(
    manifest: Manifest,
    root: pathlib.Path,
    work_directory: pathlib.Path,
    *,
    author_model: typing.Any,
    assessor_model: typing.Any,
    specs: typing.Sequence[str] = (),
) -> list[AnnotatorRun]:
    """Run the query author and then the independent assessor on each spec through Pydantic AI.

    Each annotator answers in structured output that the stages check,
    and is sent back when it names an operation the spec does not serve,
    so ``collect`` and ``build`` then run on its answers as on a hand-run annotator's.
    Every raw exchange is kept beside the answers, since a model's output cannot be reproduced.
    The two annotators should be models from different providers, so their mistakes are independent.
    """
    runs = []
    for spec in specs or list(manifest.specs):
        spec_directory = work_directory / spec
        operations_path = write_operation_list(manifest, root, spec, spec_directory)
        listing = operations_path.read_text(encoding='utf-8')
        served = {line.split('\t', 1)[0] for line in listing.splitlines()[1:]}

        author_prompt = query_author_instructions(root, spec, AUTOMATED_INPUTS) + _appendix('Operation list', listing)
        authored, author_run = await _run_annotator(
            author_model, author_prompt, list[AuthoredQuery], listing, served, spec, 'query author', spec_directory
        )
        _write_jsonl(spec_directory / QUERY_AUTHOR_OUTPUT_FILE, authored)

        queries = write_assessor_queries(spec, spec_directory)
        requests = '\n'.join(json.dumps(query, ensure_ascii=False) for query in queries)
        assessor_prompt = independent_assessor_instructions(root, spec, len(queries), AUTOMATED_INPUTS)
        assessor_prompt += _appendix('Operation list', listing) + _appendix('Requests', requests)
        assessed, assessor_run = await _run_annotator(
            assessor_model,
            assessor_prompt,
            list[AssessedQuery],
            listing,
            served,
            spec,
            'independent assessor',
            spec_directory,
            expected_ids=[query['id'] for query in queries],
        )
        _write_jsonl(spec_directory / INDEPENDENT_ASSESSOR_OUTPUT_FILE, assessed)
        runs += [author_run, assessor_run]
        (spec_directory / PROVENANCE_FILE).write_text(
            json.dumps([author_run.model_dump(), assessor_run.model_dump()], indent=2) + '\n', encoding='utf-8'
        )
    return runs


async def _run_annotator(
    model: typing.Any,
    prompt: str,
    output_type: typing.Any,
    listing: str,
    served: set[str],
    spec: str,
    role: typing.Literal['query author', 'independent assessor'],
    spec_directory: pathlib.Path,
    expected_ids: typing.Sequence[str] = (),
) -> tuple[list[typing.Any], AnnotatorRun]:
    agent = Agent(model, instructions=prompt, output_type=output_type, retries={'output': OUTPUT_RETRIES})

    @agent.tool_plain
    def find_operations(text: str) -> list[str]:
        """Lines of the operation list containing ``text``, ignoring case."""
        needle = text.lower()
        return [line for line in listing.splitlines()[1:] if needle in line.lower()][:FIND_LIMIT]

    @agent.output_validator
    def check_answer(answer: list[typing.Any]) -> list[typing.Any]:
        unknown = sorted({name for item in answer for name in item.relevant} - served)
        if unknown:
            raise ModelRetry(f'These are not operation names in the list: {unknown}. Use exact names.')
        if expected_ids:
            missing = sorted(set(expected_ids) - {item.id for item in answer})
            if missing:
                raise ModelRetry(f'Judge every request; these ids are missing: {missing}')
        else:
            missing_styles = [style for style in STYLES if not any(item.style == style for item in answer)]
            if missing_styles:
                raise ModelRetry(f'Write queries of every style; none are {missing_styles}')
        return answer

    result = await agent.run(f'Annotate the {spec} API as your instructions say.')
    (spec_directory / f'{role.replace(" ", "_")}{MESSAGES_SUFFIX}').write_bytes(result.all_messages_json())
    usage = result.usage
    run = AnnotatorRun(
        spec=spec,
        role=role,
        model=str(getattr(agent.model, 'model_name', model)),
        finished_at=datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        pydantic_ai_version=importlib.metadata.version('pydantic-ai-slim'),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model_requests=usage.requests,
    )
    return list(result.output), run


def _appendix(heading: str, body: str) -> str:
    return f'\n\n## {heading}\n\n```\n{body.rstrip()}\n```\n'


def _write_jsonl(path: pathlib.Path, records: typing.Iterable[pydantic.BaseModel]) -> None:
    path.write_text(''.join(record.model_dump_json() + '\n' for record in records), encoding='utf-8')
