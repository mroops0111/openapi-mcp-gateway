import abc
import csv
import dataclasses
import io
import pathlib
import typing

import pydantic
from pydantic_ai import Agent, ModelRetry, RunContext, capture_run_messages
from pydantic_ai.messages import ModelMessagesTypeAdapter

from ...gateway_connection import OperationListing
from ...provenance import ModelRun, model_run_fields
from ..dataset import GRADES, STYLES, Style
from ..metrics import ANSWER_GRADE, answers_in
from ..overlap import best_term_overlap, contradicts_style
from .merge import Adjudication, Annotation


# Each annotator's raw exchange, kept in the work directory, so a run can be traced after the fact.
MESSAGES_SUFFIX = '.messages.json'

# The operation list every annotator reads, one operation per line, as the first stage has always written it.
OPERATIONS_HEADER = ('name', 'method', 'path', 'summary')

# How much of a description stands in for a missing summary in the operation list.
DESCRIPTION_STAND_IN_LENGTH = 120

# How many operation lines a search of the list returns at most, enough to compare candidates without flooding.
FIND_LIMIT = 40

# How often an annotator may be sent back to fix an answer its checks refuse.
OUTPUT_RETRIES = 3

# What the query author is asked for, per spec.
QUERY_COUNT = 80
STYLE_COUNTS: dict[Style, int] = {'literal': 27, 'paraphrase': 27, 'indirect': 26}
QUERIES_PER_AREA = 6

# What an annotator is asked and answers, and the same for an annotator whose items are told apart by id.
RequestT = typing.TypeVar('RequestT', bound=pydantic.BaseModel)
AnswerT = typing.TypeVar('AnswerT', bound=pydantic.BaseModel)
IdentifiedRequestT = typing.TypeVar('IdentifiedRequestT', bound='IdentifiedQuery')
IdentifiedAnswerT = typing.TypeVar('IdentifiedAnswerT', bound='IdentifiedQuery')


@dataclasses.dataclass(frozen=True)
class SpecOperations:
    """One spec's operations, as every annotator reads them."""

    spec: str
    operation_listings: tuple[OperationListing, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(listing.name for listing in self.operation_listings)

    @property
    def summaries(self) -> dict[str, str]:
        return {listing.name: listing.summary for listing in self.operation_listings}

    def operation_list(self) -> str:
        """The list as tab-separated lines under a header, a missing summary stood in for by the description's start."""
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter='\t', lineterminator='\n')
        writer.writerow(OPERATIONS_HEADER)
        for listing in self.operation_listings:
            summary = listing.summary or listing.description[:DESCRIPTION_STAND_IN_LENGTH]
            writer.writerow([listing.name, listing.method, listing.path, summary])
        return buffer.getvalue()


@dataclasses.dataclass(frozen=True)
class AnnotatorDependencies(typing.Generic[RequestT]):
    """What one annotator's run sees, which is the spec's operations and the requests it answers, and nothing else."""

    operations: SpecOperations
    requests: tuple[RequestT, ...]


class AuthoredQuery(pydantic.BaseModel):
    """One query the query author wrote, with the author's own judgements."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    text: str = pydantic.Field(min_length=1)
    style: Style
    relevant: dict[str, int]


class IdentifiedQuery(pydantic.BaseModel):
    """A query an annotator is asked about or answers for by its id, so each answer is matched to its request."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: str


class JudgedQuery(IdentifiedQuery):
    """A query's judgements by one annotator, by its id."""

    relevant: dict[str, int]


class UnjudgedQuery(IdentifiedQuery):
    """A query as the independent assessor sees it, without the author's judgements."""

    text: str


class AssessedQuery(JudgedQuery):
    """One query the independent assessor judged."""


class DisputedQuery(IdentifiedQuery):
    """A query whose two annotators share no answer, with both their judgements."""

    text: str
    query_author: dict[str, int]
    independent_assessor: dict[str, int]


class AdjudicatedQuery(JudgedQuery):
    """The adjudicator's judgements for a disputed query, and why they fit it."""

    note: str = pydantic.Field(min_length=1)


class FlaggedQuery(IdentifiedQuery):
    """A paraphrase or indirect query that repeats too much of an answer, with the answers it must not echo."""

    text: str
    style: Style
    answers: list[str]


class RevisedQuery(IdentifiedQuery):
    """A flagged query's new text."""

    text: str = pydantic.Field(min_length=1)


class AnnotatorRun(ModelRun):
    """One annotator's run on one spec, in its role."""

    spec: str
    role: str


class Annotator(abc.ABC, typing.Generic[RequestT, AnswerT]):
    """One role in making the data, run as its own Pydantic AI agent with a context of its own.

    Every run starts with no history, so an annotator sees only the operations and the requests it is given,
    which keeps the independent assessor from ever seeing the author's answers.
    Its answer is checked by ``problems``, and the model is sent back with them until the answer passes.
    A new role is a new subclass, which the pipeline then calls.
    """

    # The role as the provenance and the work directory name it.
    role: typing.ClassVar[str]
    # The instruction template in the benchmark's prompts.
    template: typing.ClassVar[str]
    # One item of the structured answer.
    answer_type: typing.ClassVar[type[pydantic.BaseModel]]

    def __init__(self, model: typing.Any, prompts_directory: pathlib.Path):
        self._prompts_directory = prompts_directory
        self._agent: Agent[AnnotatorDependencies[RequestT], list[AnswerT]] = Agent(
            model,
            name=self.role_identifier,
            deps_type=AnnotatorDependencies,
            # The answer is a list of the role's own item, which a type checker cannot follow from a class variable.
            output_type=typing.cast(typing.Any, list[self.answer_type]),
            retries={'output': OUTPUT_RETRIES},
        )
        self._agent.tool(_find_operations)
        self._agent.output_validator(self._check_answer)

    @property
    def role_identifier(self) -> str:
        """The role as the agent and its exchange file are named, with no spaces."""
        return self.role.replace(' ', '_')

    @property
    def model_name(self) -> str:
        model = self._agent.model
        return model if isinstance(model, str) else getattr(model, 'model_name', str(model))

    async def annotate(
        self, operations: SpecOperations, requests: typing.Sequence[RequestT], work_directory: pathlib.Path
    ) -> tuple[list[AnswerT], AnnotatorRun]:
        """Answer ``requests`` on ``operations``' spec, keeping the raw exchange in ``work_directory``."""
        dependencies = AnnotatorDependencies(operations=operations, requests=tuple(requests))
        instructions = self.instructions(dependencies)
        with capture_run_messages() as messages:
            try:
                result = await self._agent.run(
                    f'Annotate the {operations.spec} API as your instructions say.',
                    deps=dependencies,
                    instructions=instructions,
                )
            finally:
                messages_path = work_directory / f'{self.role_identifier}{MESSAGES_SUFFIX}'
                messages_path.write_bytes(ModelMessagesTypeAdapter.dump_json(messages, indent=2))
        run = AnnotatorRun(
            spec=operations.spec, role=self.role, **model_run_fields(self.model_name, instructions, result.usage)
        )
        return list(result.output), run

    def instructions(self, dependencies: AnnotatorDependencies[RequestT]) -> str:
        """The rendered template, followed by the operation list and the requests."""
        template = (self._prompts_directory / self.template).read_text(encoding='utf-8')
        rendered = template.format(spec=dependencies.operations.spec, **self.template_values(dependencies))
        rendered += _appendix('Operation list', dependencies.operations.operation_list())
        if dependencies.requests:
            rendered += _appendix('Requests', '\n'.join(request.model_dump_json() for request in dependencies.requests))
        return rendered

    def template_values(self, dependencies: AnnotatorDependencies[RequestT]) -> dict[str, typing.Any]:
        """What the template names besides the spec, which is the number of requests unless a role says otherwise."""
        return {'query_count': len(dependencies.requests)}

    @abc.abstractmethod
    def problems(self, dependencies: AnnotatorDependencies[RequestT], answer: typing.Sequence[AnswerT]) -> list[str]:
        """Why ``answer`` cannot stand, each in a line the model is sent back with, or nothing when it passes."""

    def _check_answer(
        self, context: RunContext[AnnotatorDependencies[RequestT]], answer: list[AnswerT]
    ) -> list[AnswerT]:
        problems = self.problems(context.deps, answer)
        if problems:
            raise ModelRetry('\n'.join(problems))
        return answer


class FollowUpAnnotator(Annotator[IdentifiedRequestT, IdentifiedAnswerT]):
    """An annotator that revisits only the annotations it has a request for, and changes each by its answer.

    A role that follows up on the annotations further is a new subclass, which the pipeline runs after the others.
    """

    async def follow_up(
        self, operations: SpecOperations, annotations: typing.Sequence[Annotation], work_directory: pathlib.Path
    ) -> tuple[list[Annotation], AnnotatorRun | None]:
        """``annotations`` with this role's changes, and its run, or no run when it has nothing to ask."""
        requests = [
            request
            for annotation in annotations
            if (request := self.follow_up_request(operations, annotation)) is not None
        ]
        if not requests:
            return list(annotations), None
        answer, run = await self.annotate(operations, requests, work_directory)
        answer_by_id = {item.id: item for item in answer}
        followed_up = [
            self.apply_answer(annotation, answer_by_id[annotation.id]) if annotation.id in answer_by_id else annotation
            for annotation in annotations
        ]
        return followed_up, run

    @abc.abstractmethod
    def follow_up_request(self, operations: SpecOperations, annotation: Annotation) -> IdentifiedRequestT | None:
        """What this role is asked about ``annotation``, or ``None`` when it leaves it as it is."""

    @abc.abstractmethod
    def apply_answer(self, annotation: Annotation, answer: IdentifiedAnswerT) -> Annotation:
        """``annotation`` changed by this role's ``answer`` for it."""


class AuthoringAnnotator(Annotator[pydantic.BaseModel, AuthoredQuery]):
    """Writes the queries of every style from the operation list alone, with its own judgements."""

    role = 'query author'
    template = 'query_author.txt'
    answer_type = AuthoredQuery

    def template_values(self, dependencies: AnnotatorDependencies[pydantic.BaseModel]) -> dict[str, typing.Any]:
        return {
            'query_count': QUERY_COUNT,
            'queries_per_area': QUERIES_PER_AREA,
            'literal_count': STYLE_COUNTS['literal'],
            'paraphrase_count': STYLE_COUNTS['paraphrase'],
            'indirect_count': STYLE_COUNTS['indirect'],
        }

    def problems(
        self, dependencies: AnnotatorDependencies[pydantic.BaseModel], answer: typing.Sequence[AuthoredQuery]
    ) -> list[str]:
        problems = [
            problem
            for position, query in enumerate(answer, start=1)
            for problem in judgement_problems(f'Query {position}', query.relevant, dependencies.operations.names)
        ]
        missing_styles = [style for style in STYLES if not any(query.style == style for query in answer)]
        if missing_styles:
            problems.append(f'Write queries of every style; none are {missing_styles}.')
        return problems


class IndependentAssessingAnnotator(Annotator[UnjudgedQuery, AssessedQuery]):
    """Judges every query on its own, never seeing the author's answers."""

    role = 'independent assessor'
    template = 'independent_assessor.txt'
    answer_type = AssessedQuery

    def problems(
        self, dependencies: AnnotatorDependencies[UnjudgedQuery], answer: typing.Sequence[AssessedQuery]
    ) -> list[str]:
        return judged_query_problems(dependencies, answer)


class AdjudicatingAnnotator(FollowUpAnnotator[DisputedQuery, AdjudicatedQuery]):
    """Settles each query whose two annotators share no answer, having read both."""

    role = 'adjudicator'
    template = 'adjudicator.txt'
    answer_type = AdjudicatedQuery

    def follow_up_request(self, operations: SpecOperations, annotation: Annotation) -> DisputedQuery | None:
        if annotation.has_shared_answer:
            return None
        return DisputedQuery(
            id=annotation.id,
            text=annotation.text,
            query_author=annotation.annotator_a,
            independent_assessor=annotation.annotator_b,
        )

    def apply_answer(self, annotation: Annotation, answer: AdjudicatedQuery) -> Annotation:
        adjudication = Adjudication(relevant=answer.relevant, note=answer.note)
        return annotation.model_copy(update={'adjudication': adjudication})

    def problems(
        self, dependencies: AnnotatorDependencies[DisputedQuery], answer: typing.Sequence[AdjudicatedQuery]
    ) -> list[str]:
        return judged_query_problems(dependencies, answer)


class RevisingAnnotator(FollowUpAnnotator[FlaggedQuery, RevisedQuery]):
    """Rewords each paraphrase or indirect query that repeats its answer, until the style check passes it."""

    role = 'reviser'
    template = 'reviser.txt'
    answer_type = RevisedQuery

    def follow_up_request(self, operations: SpecOperations, annotation: Annotation) -> FlaggedQuery | None:
        answers = sorted(answers_in(annotation.judgements()))
        overlap = best_term_overlap(annotation.text, answers, operations.summaries)
        if not contradicts_style(annotation.style, overlap):
            return None
        return FlaggedQuery(id=annotation.id, text=annotation.text, style=annotation.style, answers=answers)

    def apply_answer(self, annotation: Annotation, answer: RevisedQuery) -> Annotation:
        return annotation.model_copy(update={'text': answer.text})

    def problems(
        self, dependencies: AnnotatorDependencies[FlaggedQuery], answer: typing.Sequence[RevisedQuery]
    ) -> list[str]:
        problems = id_problems(dependencies.requests, answer)
        flagged_by_id = {query.id: query for query in dependencies.requests}
        summaries = dependencies.operations.summaries
        for revised in answer:
            flagged = flagged_by_id.get(revised.id)
            if flagged is None:
                continue
            overlap = best_term_overlap(revised.text, flagged.answers, summaries)
            if contradicts_style(flagged.style, overlap):
                problems.append(f'{revised.id} still repeats {overlap:.0%} of an answer, so reword it further.')
        return problems


def judged_query_problems(
    dependencies: AnnotatorDependencies[IdentifiedRequestT], answer: typing.Sequence[JudgedQuery]
) -> list[str]:
    """Why judgements by id cannot stand, from a request they skip or never asked to a judgement that fails."""
    problems = id_problems(dependencies.requests, answer)
    for query in answer:
        problems += judgement_problems(query.id, query.relevant, dependencies.operations.names)
    return problems


def judgement_problems(location: str, relevant: typing.Mapping[str, int], served: typing.AbstractSet[str]) -> list[str]:
    """Why judgements cannot stand, naming an operation the spec does not serve, a wrong grade, or no answer."""
    problems = []
    unknown = sorted(set(relevant) - served)
    if unknown:
        problems.append(f'{location}: these are not operation names in the list: {unknown}. Use exact names.')
    wrong_grades = sorted({grade for grade in relevant.values() if grade not in GRADES})
    if wrong_grades:
        problems.append(f'{location}: grades {wrong_grades} are not one of {list(GRADES)}.')
    if ANSWER_GRADE not in relevant.values():
        problems.append(f'{location}: grade at least one operation {ANSWER_GRADE}.')
    return problems


def id_problems(requests: typing.Iterable[IdentifiedQuery], answer: typing.Iterable[IdentifiedQuery]) -> list[str]:
    """Which requests an answer skips or names that were never asked, by id."""
    asked = [request.id for request in requests]
    answered = [item.id for item in answer]
    problems = []
    missing = sorted(set(asked) - set(answered))
    if missing:
        problems.append(f'Answer every request; these ids are missing: {missing}.')
    unknown = sorted(set(answered) - set(asked))
    if unknown:
        problems.append(f'These ids were never asked: {unknown}.')
    return problems


def _find_operations(context: RunContext[AnnotatorDependencies[typing.Any]], text: str) -> list[str]:
    """Lines of the operation list containing ``text``, ignoring case."""
    searched_text = text.lower()
    lines = context.deps.operations.operation_list().splitlines()[1:]
    return [line for line in lines if searched_text in line.lower()][:FIND_LIMIT]


def _appendix(heading: str, body: str) -> str:
    return f'\n\n## {heading}\n\n```\n{body.rstrip()}\n```\n'
