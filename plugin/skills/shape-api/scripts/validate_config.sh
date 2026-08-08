#!/usr/bin/env bash
# validate_config.sh <config.yml>
#
# Stage 3 verifier. Boots openapi-mcp-gateway against the config. Both JSONata
# expressions compile at startup, so a broken config fails the boot with a
# message naming the side (request or response) that broke. This turns the
# gateway itself into the config validator.
#
# Exit 0 = booted clean (server stayed up until the timeout).
# Exit 1 = startup failure (the captured log holds the reason).
set -uo pipefail

if [ $# -lt 1 ]; then
  echo "usage: validate_config.sh <config.yml>" >&2
  exit 2
fi

CONFIG="$1"
LOG="$(mktemp -t gateway-boot.XXXXXX.log)"
BOOT_SECONDS="${BOOT_SECONDS:-8}"

# Prefer an installed console script; fall back to `uv run` in a checkout.
if command -v openapi-mcp-gateway >/dev/null 2>&1; then
  RUN=(openapi-mcp-gateway)
elif command -v uv >/dev/null 2>&1; then
  RUN=(uv run openapi-mcp-gateway)
else
  echo "error: openapi-mcp-gateway not found. Install it or run inside a checkout with uv." >&2
  exit 2
fi

timeout "${BOOT_SECONDS}s" "${RUN[@]}" --config "$CONFIG" >"$LOG" 2>&1
code=$?

# timeout kills a healthy long-running server with code 124, which is success here.
if [ "$code" -eq 124 ]; then
  echo "OK: config booted clean (server ran for ${BOOT_SECONDS}s without error)."
  exit 0
fi

echo "FAIL: gateway did not boot cleanly (exit ${code}). Startup log:" >&2
cat "$LOG" >&2
exit 1
