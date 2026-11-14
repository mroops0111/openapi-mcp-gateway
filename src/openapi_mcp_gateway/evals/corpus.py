import hashlib
import logging
import os
import pathlib
import tempfile

import httpx
import pydantic


logger = logging.getLogger(__name__)

# Where downloaded specs are kept between runs, unless OPENAPI_MCP_GATEWAY_CACHE names another directory.
_CACHE_ENV = 'OPENAPI_MCP_GATEWAY_CACHE'
_DEFAULT_CACHE = pathlib.Path('~/.cache/openapi-mcp-gateway').expanduser()

# Specs run to tens of megabytes, so a download gets longer than an ordinary request.
_DOWNLOAD_TIMEOUT_SECONDS = 120


class SpecPin(pydantic.BaseModel):
    """One OpenAPI document fixed to an exact revision, so a benchmark never measures a moving target.

    ``source`` is an HTTP(S) URL that names the revision, such as a raw file at a commit,
    or a path relative to the file that declares the pin.
    ``sha256`` is checked on every use, so a source that changes in place is refused rather than measured.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    source: str
    sha256: str = pydantic.Field(pattern=r'^[0-9a-f]{64}$')

    @property
    def is_remote(self) -> bool:
        return self.source.startswith(('http://', 'https://'))


class SpecChecksumError(ValueError):
    """A spec's content does not match the checksum its pin declares."""


def cache_dir() -> pathlib.Path:
    """The directory downloaded specs are kept in."""
    configured = os.environ.get(_CACHE_ENV)
    return pathlib.Path(configured).expanduser() if configured else _DEFAULT_CACHE


def fetch(pin: SpecPin, base_dir: pathlib.Path) -> pathlib.Path:
    """Return a local file holding the pinned spec, downloading it into the cache when it is not there yet.

    A local ``source`` resolves against ``base_dir``.
    Raises ``SpecChecksumError`` when the content does not match ``pin.sha256``.
    """
    if not pin.is_remote:
        path = (base_dir / pin.source).resolve()
        _verify(path.read_bytes(), pin, str(path))
        return path

    suffix = pathlib.PurePosixPath(httpx.URL(pin.source).path).suffix or '.json'
    cached = cache_dir() / 'specs' / f'{pin.sha256}{suffix}'
    if cached.exists():
        _verify(cached.read_bytes(), pin, str(cached))
        return cached

    logger.info('Downloading %s', pin.source)
    response = httpx.get(pin.source, timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    _verify(response.content, pin, pin.source)
    cached.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the target and renamed into place, so an interrupted download never leaves a partial file.
    with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as partial:
        partial.write(response.content)
    pathlib.Path(partial.name).replace(cached)
    return cached


def sha256_of(path: pathlib.Path) -> str:
    """The hex SHA-256 of a file, for writing a new pin."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify(content: bytes, pin: SpecPin, where: str) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != pin.sha256:
        raise SpecChecksumError(
            f'{where} has SHA-256 {actual}, but its pin declares {pin.sha256}. '
            'Point the pin at an exact revision, or update the checksum and treat it as a new benchmark version.'
        )
