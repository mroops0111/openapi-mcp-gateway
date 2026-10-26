# Runs the gateway as a service from the wheel in dist/.
# Build the wheel first: `uv build --wheel && docker build -t openapi-mcp-gateway .`
# The release workflow builds this image from the same wheel it publishes to PyPI.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The redis and oidc extras ship in the one image,
# so every auth mode and the shared store work without a rebuild.
RUN --mount=type=bind,source=dist,target=/dist \
    set -eu; \
    wheels=$(ls /dist/openapi_mcp_gateway-*.whl); \
    [ "$(echo "$wheels" | wc -l)" -eq 1 ] || { echo "expected exactly one wheel in dist/, found: $wheels" >&2; exit 1; }; \
    uv pip install --system --no-cache "${wheels}[redis,oidc]"

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway
USER gateway

EXPOSE 8000

# Probes the default port; a config that binds another port should override this check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

ENTRYPOINT ["openapi-mcp-gateway"]
CMD ["--config", "/config/config.yml"]
