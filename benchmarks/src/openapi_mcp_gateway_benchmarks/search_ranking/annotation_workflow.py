import collections
import csv
import json
import pathlib
import shutil
import typing

import pydantic

from ..gateway_connection import OperationListing, list_operations
from ..provenance import provenance_path
from .annotations import ANNOTATIONS_FILE, Annotation, annotations_path, read_annotations
from .dataset import GRADES, PROMPTS_DIRECTORY, STYLES, DatasetError, Manifest, Style
from .metrics import ANSWER_GRADE
from .runner import corpus_server


# The instruction templates, kept with the benchmark's data, so a change to them is a change to the data.
QUERY_AUTHOR_TEMPLATE = 'query_author.txt'
INDEPENDENT_ASSESSOR_TEMPLATE = 'independent_assessor.txt'

# What each stage writes into a spec's directory of the work directory, and what it expects back.
OPERATIONS_FILE = 'operations.tsv'
OPERATIONS_HEADER = ['name', 'method', 'path', 'summary']
QUERY_AUTHOR_INSTRUCTIONS_FILE = 'query_author_instructions.txt'
QUERY_AUTHOR_OUTPUT_FILE = 'query_author.jsonl'
ASSESSOR_QUERIES_FILE = 'queries.jsonl'
INDEPENDENT_ASSESSOR_INSTRUCTIONS_FILE = 'independent_assessor_instructions.txt'
INDEPENDENT_ASSESSOR_OUTPUT_FILE = 'independent_assessor.jsonl'

# How much of a description stands in for a missing summary in the operation list.
DESCRIPTION_STAND_IN_LENGTH = 120

# What the query author is asked for, per spec.
QUERY_COUNT = 80
STYLE_COUNTS: dict[Style, int] = {'literal': 27, 'paraphrase': 27, 'indirect': 26}
QUERIES_PER_AREA = 6

# What each annotator writes, one object per line, when it is run by hand.
MANUAL_OUTPUT_EXAMPLES = {
    QUERY_AUTHOR_OUTPUT_FILE: '{"text": "...", "style": "literal", "relevant": {"operation_name": 2, "other_operation_name": 1}}',
    INDEPENDENT_ASSESSOR_OUTPUT_FILE: '{"id": "...", "relevant": {"operation_name": 2}}',
}

RecordType = typing.TypeVar('RecordType', bound=pydantic.BaseModel)


class InstructionInputs(pydantic.BaseModel):
    """What an annotator's instructions say about where its input comes from and where its answer goes.

    An annotator run by hand reads files and writes one,
    and one run through Pydantic AI reads the prompt and answers in structured output, so the same template serves both.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    operations_input: str
    queries_input: str
    search_note: str
    output_instructions: str


class AuthoredQuery(pydantic.BaseModel):
    """One query the query author wrote, with the author's own judgements."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    text: str = pydantic.Field(min_length=1)
    style: Style
    relevant: dict[str, int]


class AssessedQuery(pydantic.BaseModel):
    """One query the independent assessor judged, by its id."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: str
    relevant: dict[str, int]


def prepare_authoring(manifest: Manifest, root: pathlib.Path, work_directory: pathlib.Path) -> list[pathlib.Path]:
    """Write each spec's operation list and the query author's instructions, returning the instruction files."""
    instruction_files = []
    for spec in manifest.specs:
        spec_directory = work_directory / spec
        write_operation_list(manifest, root, spec, spec_directory)
        instructions = query_author_instructions(root, spec, _manual_inputs(spec_directory, QUERY_AUTHOR_OUTPUT_FILE))
        instruction_files.append(_write_text(spec_directory / QUERY_AUTHOR_INSTRUCTIONS_FILE, instructions))
    return instruction_files


def prepare_assessment(manifest: Manifest, root: pathlib.Path, work_directory: pathlib.Path) -> list[pathlib.Path]:
    """Write each spec's queries, without the author's judgements, and the assessor's instructions.

    Each query takes its id from its line in the author's file, so the same file always gives the same ids.
    """
    instruction_files = []
    for spec in manifest.specs:
        spec_directory = work_directory / spec
        query_count = len(write_assessor_queries(spec, spec_directory))
        inputs = _manual_inputs(spec_directory, INDEPENDENT_ASSESSOR_OUTPUT_FILE)
        instructions = independent_assessor_instructions(root, spec, query_count, inputs)
        instruction_files.append(_write_text(spec_directory / INDEPENDENT_ASSESSOR_INSTRUCTIONS_FILE, instructions))
    return instruction_files


def write_operation_list(
    manifest: Manifest, root: pathlib.Path, spec: str, spec_directory: pathlib.Path
) -> pathlib.Path:
    """Write ``spec``'s operation list, as the annotators read it, into ``spec_directory``."""
    spec_directory.mkdir(parents=True, exist_ok=True)
    path = spec_directory / OPERATIONS_FILE
    _write_operations(path, list_operations(corpus_server(manifest, root, spec)))
    return path


def write_assessor_queries(spec: str, spec_directory: pathlib.Path) -> list[dict[str, str]]:
    """Give each authored query its id and write the queries the assessor sees, without the author's judgements."""
    authored_queries = _read_records(spec_directory / QUERY_AUTHOR_OUTPUT_FILE, AuthoredQuery)
    queries = [
        {'id': query_id, 'text': authored_query.text}
        for query_id, authored_query in zip(_query_ids(spec, len(authored_queries)), authored_queries, strict=True)
    ]
    lines = [json.dumps(query, ensure_ascii=False) for query in queries]
    _write_text(spec_directory / ASSESSOR_QUERIES_FILE, '\n'.join(lines) + '\n')
    return queries


def query_author_instructions(root: pathlib.Path, spec: str, inputs: InstructionInputs) -> str:
    """The query author's instructions for ``spec``, rendered from its template in the prompts."""
    return _render(
        root / PROMPTS_DIRECTORY / QUERY_AUTHOR_TEMPLATE,
        spec=spec,
        query_count=QUERY_COUNT,
        queries_per_area=QUERIES_PER_AREA,
        literal_count=STYLE_COUNTS['literal'],
        paraphrase_count=STYLE_COUNTS['paraphrase'],
        indirect_count=STYLE_COUNTS['indirect'],
        **inputs.model_dump(),
    )


def independent_assessor_instructions(
    root: pathlib.Path, spec: str, query_count: int, inputs: InstructionInputs
) -> str:
    """The independent assessor's instructions for ``spec``, rendered from its template in the prompts."""
    return _render(
        root / PROMPTS_DIRECTORY / INDEPENDENT_ASSESSOR_TEMPLATE,
        spec=spec,
        query_count=query_count,
        **inputs.model_dump(),
    )


def collect(manifest: Manifest, root: pathlib.Path, work_directory: pathlib.Path) -> dict[str, int]:
    """Check both annotators' output for each spec and write it as the spec's annotations, returning query counts.

    Raises ``DatasetError`` when an annotator names an operation the spec does not serve, gives a grade outside 1 and 2,
    or leaves a query without an answer, so a mistake is caught before it reaches the judgements.
    Nothing is written until every spec passes, so a failure leaves the annotations as they were.
    The automated annotators' record of their runs goes with the annotations, and a hand-run annotation has none.
    An adjudication already in a spec's file is kept when its query's text and judgements are unchanged,
    since it settled exactly that disagreement.
    """
    annotations_by_spec: dict[str, list[Annotation]] = {}
    for spec in manifest.specs:
        spec_directory = work_directory / spec
        served = {row['name'] for row in _read_operations(spec_directory / OPERATIONS_FILE)}
        authored_queries = _read_records(spec_directory / QUERY_AUTHOR_OUTPUT_FILE, AuthoredQuery)
        assessed_queries = {
            assessed.id: assessed
            for assessed in _read_records(spec_directory / INDEPENDENT_ASSESSOR_OUTPUT_FILE, AssessedQuery)
        }
        existing = _existing_annotations(annotations_path(root, spec))
        annotations = []
        for query_id, authored_query in zip(_query_ids(spec, len(authored_queries)), authored_queries, strict=True):
            if query_id not in assessed_queries:
                raise DatasetError(f'{spec}: the independent assessor did not judge {query_id}')
            for annotator, relevant in (
                ('query author', authored_query.relevant),
                ('independent assessor', assessed_queries[query_id].relevant),
            ):
                _check_judgements(f'{spec} {query_id}, {annotator}', relevant, served)
            annotation = Annotation(
                id=query_id,
                text=authored_query.text,
                style=authored_query.style,
                annotator_a=authored_query.relevant,
                annotator_b=assessed_queries[query_id].relevant,
            )
            annotations.append(_with_kept_adjudication(annotation, existing.get(query_id)))
        style_counts = collections.Counter(annotation.style for annotation in annotations)
        missing_styles = [style for style in STYLES if not style_counts[style]]
        if missing_styles:
            raise DatasetError(f'{spec}: the query author wrote no {", ".join(missing_styles)} queries')
        annotations_by_spec[spec] = annotations
    for spec, annotations in annotations_by_spec.items():
        lines = [annotation.model_dump_json(exclude_none=True) for annotation in annotations]
        _write_text(annotations_path(root, spec), '\n'.join(lines) + '\n')
        _carry_provenance(work_directory / spec / ANNOTATIONS_FILE, annotations_path(root, spec))
    return {spec: len(annotations) for spec, annotations in annotations_by_spec.items()}


def _carry_provenance(answers: pathlib.Path, collected: pathlib.Path) -> None:
    """Keep the automated annotators' record beside the annotations, or none when they were run by hand."""
    record = provenance_path(answers)
    if record.exists():
        shutil.copyfile(record, provenance_path(collected))
    else:
        provenance_path(collected).unlink(missing_ok=True)


def _existing_annotations(path: pathlib.Path) -> dict[str, Annotation]:
    if not path.exists():
        return {}
    return {annotation.id: annotation for annotation in read_annotations(path)}


def _with_kept_adjudication(annotation: Annotation, existing: Annotation | None) -> Annotation:
    if existing is None or existing.adjudication is None:
        return annotation
    unchanged = (existing.text, existing.annotator_a, existing.annotator_b) == (
        annotation.text,
        annotation.annotator_a,
        annotation.annotator_b,
    )
    return annotation.model_copy(update={'adjudication': existing.adjudication}) if unchanged else annotation


def _check_judgements(location: str, relevant: typing.Mapping[str, int], served: set[str]) -> None:
    unknown = sorted(set(relevant) - served)
    if unknown:
        raise DatasetError(f'{location}: {unknown} are not operations the spec serves')
    wrong_grades = sorted(grade for grade in relevant.values() if grade not in GRADES)
    if wrong_grades:
        raise DatasetError(f'{location}: grades {wrong_grades} are not one of {list(GRADES)}')
    if ANSWER_GRADE not in relevant.values():
        raise DatasetError(f'{location}: no operation is graded {ANSWER_GRADE}')


def _manual_inputs(spec_directory: pathlib.Path, output_file: str) -> InstructionInputs:
    example = MANUAL_OUTPUT_EXAMPLES[output_file]
    return InstructionInputs(
        operations_input=str(spec_directory / OPERATIONS_FILE),
        queries_input=str(spec_directory / ASSESSOR_QUERIES_FILE),
        search_note='You may read and search those files.',
        output_instructions=(
            f'Write {spec_directory / output_file} as JSON Lines, one object per line:\n\n{example}\n\n'
            'Every key of relevant must be an exact name from the operation file, so check each one against it.'
        ),
    )


def _query_ids(spec: str, count: int) -> list[str]:
    return [f'{spec}-q{number:03d}' for number in range(1, count + 1)]


def _write_operations(path: pathlib.Path, listings: typing.Iterable[OperationListing]) -> None:
    with path.open('w', encoding='utf-8', newline='') as file:
        writer = csv.writer(file, delimiter='\t', lineterminator='\n')
        writer.writerow(OPERATIONS_HEADER)
        for listing in listings:
            listed_summary = listing.summary or listing.description[:DESCRIPTION_STAND_IN_LENGTH]
            writer.writerow([listing.name, listing.method, listing.path, listed_summary])


def _read_operations(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8', newline='') as file:
        return list(csv.DictReader(file, delimiter='\t'))


def _read_records(path: pathlib.Path, model: type[RecordType]) -> list[RecordType]:
    if not path.exists():
        raise DatasetError(f'{path} not found, so the stage before this one has not run')
    records = []
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate(json.loads(line)))
        except (json.JSONDecodeError, pydantic.ValidationError) as error:
            raise DatasetError(f'{path}:{line_number}: {error}') from error
    return records


def _render(template: pathlib.Path, **values: typing.Any) -> str:
    return template.read_text(encoding='utf-8').format(**values)


def _write_text(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding='utf-8')
    return path
