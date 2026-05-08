import json
import logging
import typing

import httpx


logger = logging.getLogger(__name__)


class APIClient:
    """Thin async HTTP wrapper around ``httpx.AsyncClient`` for upstream calls."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Configure base URL, default headers, timeout, and optional async transport.

        ``transport`` lets callers route requests somewhere other than the
        network — typically an ``httpx.ASGITransport`` for in-process FastAPI
        integration or an ``httpx.MockTransport`` in tests.
        """
        client_kwargs: dict[str, typing.Any] = {
            'base_url': base_url,
            'timeout': timeout,
            'headers': headers or {},
        }
        if transport is not None:
            client_kwargs['transport'] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> typing.Self:
        """Enter async context manager scope."""
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        """Close the client when leaving context."""
        await self.aclose()

    def set_auth_header(self, token: str, scheme: str = 'Bearer') -> None:
        """Set the ``Authorization`` header using ``scheme`` and ``token``."""
        self._client.headers['Authorization'] = f'{scheme} {token}'

    def set_header(self, key: str, value: str) -> None:
        """Set an arbitrary default header on the underlying client."""
        self._client.headers[key] = value

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, typing.Any] | None = None,
        data: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, typing.Any]:
        """Perform ``method`` against ``path`` (relative to ``base_url``).

        JSON bodies use the ``json`` parameter for POST/PUT/PATCH. Raises
        ``httpx.HTTPStatusError`` on HTTP error responses.

        Returns:
            Decoded JSON object for ``application/json``, ``{'status': code}``
            for empty 204 bodies, or ``{'data': text}`` for other media types.
        """
        request_kwargs: dict[str, typing.Any] = {}
        if params:
            request_kwargs['params'] = params
        if data is not None and method.upper() in ('POST', 'PUT', 'PATCH'):
            request_kwargs['json'] = data
        if headers:
            request_kwargs['headers'] = headers

        logger.debug('Upstream request: %s %s', method.upper(), path)
        response = await self._client.request(method.upper(), path, **request_kwargs)
        if response.is_error:
            logger.warning(
                'Upstream error: %s %s → %d %s',
                method.upper(),
                response.request.url,
                response.status_code,
                response.reason_phrase,
            )
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase} for '{response.request.url}': {response.text}",
                request=response.request,
                response=response,
            )
        logger.debug('Upstream response: %s %s → %d', method.upper(), path, response.status_code)

        if response.status_code == 204 or not response.content:
            return {'status': response.status_code}

        content_type = response.headers.get('content-type', '')
        if 'application/json' in content_type:
            try:
                return response.json()
            except json.JSONDecodeError:
                return {'data': response.text}
        return {'data': response.text}
