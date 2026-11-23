import hashlib
import logging
import os
import pathlib
import tempfile

import httpx
import pydantic


logger = logging.getLogger(__name__)

# Where downloaded specs are kept between runs, unless OPENAPI_MCP_GATEWAY_CACHE names another directory.
CACHE_DIRECTORY_VARIABLE = 'OPENAPI_MCP_GATEWAY_CACHE'
DEFAULT_CACHE_DIRECTORY = pathlib.Path('~/.cache/openapi-mcp-gateway').expanduser()

# Specs run to tens of megabytes, so a download gets longer than an ordinary request.
DOWNLOAD_TIMEOUT_SECONDS = 120


class SpecChecksumError(ValueError):
    """A spec's content does not match the checksum its pin declares."""


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

    def fetch(self, base_directory: pathlib.Path) -> pathlib.Path:
        """Return a local file holding the pinned spec, downloading it into the cache when it is not there yet.

        A local ``source`` resolves against ``base_directory``.
        Raises ``SpecChecksumError`` when the content does not match ``sha256``.
        """
        if not self.is_remote:
            path = (base_directory / self.source).resolve()
            self._verify(path.read_bytes(), str(path))
            return path

        cached = _cached_path(self.source, self.sha256)
        if cached.exists():
            self._verify(cached.read_bytes(), str(cached))
            return cached

        content = _download(self.source)
        self._verify(content, self.source)
        _write_cache(cached, content)
        return cached

    def _verify(self, content: bytes, location: str) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != self.sha256:
            raise SpecChecksumError(
                f'{location} has SHA-256 {actual}, but its pin declares {self.sha256}. '
                'Point the pin at an exact revision, or update the checksum and treat it as a new benchmark version.'
            )


def pin_as_it_is(source: str, base_directory: pathlib.Path) -> SpecPin:
    """Pin ``source`` to its content now, downloading a URL once into the cache, for a spec no pin fixes yet.

    A user's own config names its spec without a checksum, so this holds it still for the length of one run.
    A local ``source`` resolves against ``base_directory``.
    """
    if not source.startswith(('http://', 'https://')):
        path = (base_directory / source).expanduser().resolve()
        return SpecPin(source=str(path), sha256=sha256_of(path))
    content = _download(source)
    pin = SpecPin(source=source, sha256=hashlib.sha256(content).hexdigest())
    _write_cache(_cached_path(source, pin.sha256), content)
    return pin


def cache_directory() -> pathlib.Path:
    """The directory downloaded specs are kept in."""
    configured = os.environ.get(CACHE_DIRECTORY_VARIABLE)
    return pathlib.Path(configured).expanduser() if configured else DEFAULT_CACHE_DIRECTORY


def sha256_of(path: pathlib.Path) -> str:
    """The hex SHA-256 of a file, for writing a new pin."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cached_path(source: str, sha256: str) -> pathlib.Path:
    suffix = pathlib.PurePosixPath(httpx.URL(source).path).suffix or '.json'
    return cache_directory() / 'specs' / f'{sha256}{suffix}'


def _download(source: str) -> bytes:
    logger.info('Downloading %s', source)
    response = httpx.get(source, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _write_cache(cached: pathlib.Path, content: bytes) -> None:
    cached.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the target and renamed into place, so an interrupted download never leaves a partial file.
    with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as partial:
        partial.write(content)
    pathlib.Path(partial.name).replace(cached)
