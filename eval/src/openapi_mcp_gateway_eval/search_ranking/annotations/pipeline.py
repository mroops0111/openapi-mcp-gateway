import asyncio
import dataclasses
import pathlib
import typing

from ...gateway_connection import list_operations
from ...provenance import write_provenance
from ..dataset import PROMPTS_DIRECTORY, Manifest
from ..runner import corpus_server
from .annotators import (
    Adjudicator,
    AnnotatorRun,
    IndependentAssessor,
    QueryAuthor,
    Refiner,
    Reviser,
    SpecOperations,
    UnjudgedQuery,
)
from .merge import Annotation, annotations_path, write_annotations


# How many specs are annotated at once, few enough to stay under a provider's rate limit.
SPEC_CONCURRENCY = 4

# The operation list each spec's annotators read, kept in the work directory beside their exchanges.
OPERATIONS_FILE = 'operations.tsv'


@dataclasses.dataclass(frozen=True)
class SpecAnnotation:
    """One spec's annotations as the pipeline made them, with every annotator run that went into them."""

    spec: str
    annotations: list[Annotation]
    runs: list[AnnotatorRun]


class AnnotationPipeline:
    """Makes each spec's annotations anew, from the operation list to queries no style check refuses.

    Each spec runs its annotators in turn, since each reads what the one before it wrote.
    The query author writes the queries and the independent assessor judges them without the author's answers.
    Then each refiner revisits the annotations it has a request for.
    The adjudicator settles any query the two share no answer for,
    and the reviser, the author's model, rewords any paraphrase or indirect query that repeats its answer.
    Specs run side by side, and nothing is written until every spec has finished,
    so a failure leaves the data as it was.
    """

    def __init__(
        self,
        manifest: Manifest,
        root: pathlib.Path,
        *,
        author_model: typing.Any,
        assessor_model: typing.Any,
        adjudicator_model: typing.Any,
    ):
        prompts_directory = root / PROMPTS_DIRECTORY
        self._manifest = manifest
        self._root = root
        self._author = QueryAuthor(author_model, prompts_directory)
        self._assessor = IndependentAssessor(assessor_model, prompts_directory)
        # In the order they run, since the reviser reads the judgements the adjudicator settles.
        self._refiners: list[Refiner[typing.Any, typing.Any]] = [
            Adjudicator(adjudicator_model, prompts_directory),
            Reviser(author_model, prompts_directory),
        ]

    async def run(self, work_directory: pathlib.Path, specs: typing.Sequence[str] = ()) -> list[SpecAnnotation]:
        """Annotate ``specs``, or every spec, keeping each raw exchange in ``work_directory``, then write them."""
        semaphore = asyncio.Semaphore(SPEC_CONCURRENCY)

        async def annotate_one(spec: str) -> SpecAnnotation:
            async with semaphore:
                return await self.annotate_spec(spec, work_directory / spec)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(annotate_one(spec)) for spec in specs or list(self._manifest.specs)]
        except ExceptionGroup as failures:
            # One spec's failure cancels the rest, and the command reports the first as it would any error.
            raise failures.exceptions[0] from failures
        spec_annotations = [task.result() for task in tasks]
        for spec_annotation in spec_annotations:
            write_annotations(self._root, spec_annotation.spec, spec_annotation.annotations)
            write_provenance(annotations_path(self._root, spec_annotation.spec), spec_annotation.runs)
        return spec_annotations

    async def annotate_spec(self, spec: str, work_directory: pathlib.Path) -> SpecAnnotation:
        """One spec's annotations, written nowhere but the raw exchanges in ``work_directory``."""
        work_directory.mkdir(parents=True, exist_ok=True)
        operations = await self._spec_operations(spec)
        (work_directory / OPERATIONS_FILE).write_text(operations.operation_list(), encoding='utf-8')
        annotations, runs = await self._annotate_queries(operations, work_directory)
        for refiner in self._refiners:
            annotations, refiner_run = await refiner.refine(operations, annotations, work_directory)
            if refiner_run is not None:
                runs.append(refiner_run)
        return SpecAnnotation(spec=spec, annotations=annotations, runs=runs)

    async def _spec_operations(self, spec: str) -> SpecOperations:
        # Loading a spec blocks, so it runs off the event loop while the other specs' annotators wait on their models.
        server = corpus_server(self._manifest, self._root, spec)
        operation_listings = await asyncio.to_thread(list_operations, server)
        return SpecOperations(spec, tuple(operation_listings))

    async def _annotate_queries(
        self, operations: SpecOperations, work_directory: pathlib.Path
    ) -> tuple[list[Annotation], list[AnnotatorRun]]:
        """The author's queries with both annotators' judgements, and the two runs that made them."""
        authored, author_run = await self._author.annotate(operations, (), work_directory)
        query_ids = [_query_id(operations.spec, number) for number in range(1, len(authored) + 1)]
        unjudged = [
            UnjudgedQuery(id=query_id, text=query.text) for query_id, query in zip(query_ids, authored, strict=True)
        ]
        assessed, assessor_run = await self._assessor.annotate(operations, unjudged, work_directory)
        assessed_by_id = {query.id: query for query in assessed}
        annotations = [
            Annotation(
                id=query_id,
                text=query.text,
                style=query.style,
                annotator_a=query.relevant,
                annotator_b=assessed_by_id[query_id].relevant,
            )
            for query_id, query in zip(query_ids, authored, strict=True)
        ]
        return annotations, [author_run, assessor_run]


def _query_id(spec: str, number: int) -> str:
    return f'{spec}-q{number:03d}'
