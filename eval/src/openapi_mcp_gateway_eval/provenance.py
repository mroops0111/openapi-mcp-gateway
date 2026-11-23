import datetime
import hashlib
import importlib.metadata
import json
import pathlib
import typing

import pydantic
from pydantic_ai.usage import RunUsage


# A file a model wrote keeps the record of that run beside it, named after it with this suffix.
PROVENANCE_SUFFIX = '.provenance.json'


class ModelRun(pydantic.BaseModel):
    """A model's run that wrote benchmark data, saying which model ran, from which instructions, when, and at what cost.

    Each benchmark adds what tells its runs apart, since a model's output cannot be reproduced, only traced.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    model: str
    instructions_sha256: str
    finished_at: str
    pydantic_ai_version: str
    input_tokens: int
    output_tokens: int
    model_requests: int


def model_run_fields(model: str, instructions: str, usage: RunUsage) -> dict[str, typing.Any]:
    """The fields every ``ModelRun`` shares, for a run of ``model`` given ``instructions`` that just finished."""
    return {
        'model': model,
        'instructions_sha256': hashlib.sha256(instructions.encode()).hexdigest(),
        'finished_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        'pydantic_ai_version': importlib.metadata.version('pydantic-ai-slim'),
        'input_tokens': usage.input_tokens,
        'output_tokens': usage.output_tokens,
        'model_requests': usage.requests,
    }


def provenance_path(data_path: pathlib.Path) -> pathlib.Path:
    """Where the record of the run that wrote ``data_path`` is kept, beside it."""
    return data_path.with_name(data_path.stem + PROVENANCE_SUFFIX)


def write_provenance(data_path: pathlib.Path, runs: typing.Sequence[ModelRun]) -> pathlib.Path:
    """Write the runs that wrote ``data_path`` beside it, one record per run."""
    path = provenance_path(data_path)
    records = [run.model_dump() for run in runs]
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return path
