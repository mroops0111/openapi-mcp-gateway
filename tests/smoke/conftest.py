import pathlib
import subprocess
import sys
import tempfile

import pytest


_FASTAPI_REPO = 'https://github.com/fastapi/fastapi.git'
_CLONE_PATH = pathlib.Path(tempfile.gettempdir()) / 'openapi-mcp-gateway-smoke-fastapi'


@pytest.fixture(scope='session')
def fastapi_docs_src() -> pathlib.Path:
    """Clone fastapi/fastapi (depth=1) once per session and expose ``docs_src``.

    Reuses the existing clone on subsequent runs.
    The clone path lives in the OS temp dir so it is isolated from the project tree.
    """
    if not (_CLONE_PATH / 'docs_src').exists():
        _CLONE_PATH.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ['git', 'clone', '--depth', '1', _FASTAPI_REPO, str(_CLONE_PATH)],
            check=True,
        )
    if str(_CLONE_PATH) not in sys.path:
        sys.path.insert(0, str(_CLONE_PATH))
    return _CLONE_PATH / 'docs_src'
