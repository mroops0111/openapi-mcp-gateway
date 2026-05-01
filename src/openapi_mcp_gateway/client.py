import json
import logging
import typing

import httpx


logger = logging.getLogger(__name__)


class APIClient:
    """Async HTTP client that wraps httpx for upstream API requests."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 90,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=headers or {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        await self.aclose()

    def set_auth_header(self, token: str, scheme: str = 'Bearer') -> None:
        self._client.headers['Authorization'] = f'{scheme} {token}'

    def set_header(self, key: str, value: str) -> None:
        self._client.headers[key] = value

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, typing.Any] | None = None,
        data: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, typing.Any]:
        """Make an HTTP request to the upstream API.

        Returns:
            Parsed response. JSON responses are returned as-is,
            other content types are wrapped as {'data': text}.
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
