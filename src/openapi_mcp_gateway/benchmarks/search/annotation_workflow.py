import collections
import csv
import json
import pathlib
import typing

import pydantic

from ..gateway_connection import OperationListing, list_operations
from .annotations import ANNOTATIONS_DIRECTORY, Annotation
from .dataset import GRADES, STYLES, DatasetError, Manifest, Style
from .metrics import ANSWER_GRADE
from .runner import corpus_server


# The instruction templates, kept beside the annotations they produced, so a change to them is a change to the data.
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

RecordType = typing.TypeVar('RecordType', bound=pydantic.BaseModel)


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
        spec_directory.mkdir(parents=True, exist_ok=True)
        _write_operations(spec_directory / OPERATIONS_FILE, list_operations(corpus_server(manifest, root, spec)))
        instructions = _render(
            root / ANNOTATIONS_DIRECTORY / QUERY_AUTHOR_TEMPLATE,
            spec=spec,
            operations_file=spec_directory / OPERATIONS_FILE,
            output_file=spec_directory / QUERY_AUTHOR_OUTPUT_FILE,
            query_count=QUERY_COUNT,
            queries_per_area=QUERIES_PER_AREA,
            literal_count=STYLE_COUNTS['literal'],
            paraphrase_count=STYLE_COUNTS['paraphrase'],
            indirect_count=STYLE_COUNTS['indirect'],
        )
        instruction_files.append(_write_text(spec_directory / QUERY_AUTHOR_INSTRUCTIONS_FILE, instructions))
    return instruction_files


def prepare_assessment(manifest: Manifest, root: pathlib.Path, work_directory: pathlib.Path) -> list[pathlib.Path]:
    """Write each spec's queries, without the author's judgements, and the assessor's instructions.

    Each query takes its id from its line in the author's file, so the same file always gives the same ids.
    """
    instruction_files = []
    for spec in manifest.specs:
        spec_directory = work_directory / spec
        authored_queries = _read_records(spec_directory / QUERY_AUTHOR_OUTPUT_FILE, AuthoredQuery)
        with (spec_directory / ASSESSOR_QUERIES_FILE).open('w', encoding='utf-8') as file:
            for query_id, authored_query in zip(_query_ids(spec, len(authored_queries)), authored_queries, strict=True):
                file.write(json.dumps({'id': query_id, 'text': authored_query.text}, ensure_ascii=False) + '\n')
        instructions = _render(
            root / ANNOTATIONS_DIRECTORY / INDEPENDENT_ASSESSOR_TEMPLATE,
            spec=spec,
            operations_file=spec_directory / OPERATIONS_FILE,
            queries_file=spec_directory / ASSESSOR_QUERIES_FILE,
            output_file=spec_directory / INDEPENDENT_ASSESSOR_OUTPUT_FILE,
            query_count=len(authored_queries),
        )
        instruction_files.append(_write_text(spec_directory / INDEPENDENT_ASSESSOR_INSTRUCTIONS_FILE, instructions))
    return instruction_files


def collect(manifest: Manifest, root: pathlib.Path, work_directory: pathlib.Path) -> dict[str, int]:
    """Check both annotators' output for each spec and write it as the spec's annotations, returning query counts.

    Raises ``DatasetError`` when an annotator names an operation the spec does not serve, gives a grade outside 1 and 2,
    or leaves a query without an answer, so a mistake is caught before it reaches the judgements.
    """
    query_counts = {}
    for spec in manifest.specs:
        spec_directory = work_directory / spec
        served = {row['name'] for row in _read_operations(spec_directory / OPERATIONS_FILE)}
        authored_queries = _read_records(spec_directory / QUERY_AUTHOR_OUTPUT_FILE, AuthoredQuery)
        assessed_queries = {
            assessed.id: assessed
            for assessed in _read_records(spec_directory / INDEPENDENT_ASSESSOR_OUTPUT_FILE, AssessedQuery)
        }
        annotations = []
        for query_id, authored_query in zip(_query_ids(spec, len(authored_queries)), authored_queries, strict=True):
            if query_id not in assessed_queries:
                raise DatasetError(f'{spec}: the independent assessor did not judge {query_id}')
            for annotator, relevant in (
                ('query author', authored_query.relevant),
                ('independent assessor', assessed_queries[query_id].relevant),
            ):
                _check_judgements(f'{spec} {query_id}, {annotator}', relevant, served)
            annotations.append(
                Annotation(
                    id=query_id,
                    text=authored_query.text,
                    style=authored_query.style,
                    annotator_a=authored_query.relevant,
                    annotator_b=assessed_queries[query_id].relevant,
                )
            )
        style_counts = collections.Counter(annotation.style for annotation in annotations)
        missing_styles = [style for style in STYLES if not style_counts[style]]
        if missing_styles:
            raise DatasetError(f'{spec}: the query author wrote no {", ".join(missing_styles)} queries')
        lines = [annotation.model_dump_json(exclude_none=True) for annotation in annotations]
        _write_text(root / ANNOTATIONS_DIRECTORY / f'{spec}.jsonl', '\n'.join(lines) + '\n')
        query_counts[spec] = len(annotations)
    return query_counts


def _check_judgements(location: str, relevant: typing.Mapping[str, int], served: set[str]) -> None:
    unknown = sorted(set(relevant) - served)
    if unknown:
        raise DatasetError(f'{location}: {unknown} are not operations the spec serves')
    wrong_grades = sorted(grade for grade in relevant.values() if grade not in GRADES)
    if wrong_grades:
        raise DatasetError(f'{location}: grades {wrong_grades} are not one of {list(GRADES)}')
    if ANSWER_GRADE not in relevant.values():
        raise DatasetError(f'{location}: no operation is graded {ANSWER_GRADE}')


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
