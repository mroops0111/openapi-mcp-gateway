#!/usr/bin/env bash
# fetch_spec.sh <url-or-path>
#
# Stage 1 helper. Fetch an OpenAPI spec candidate from a URL or local path,
# confirm it parses, and report whether it looks like OpenAPI/Swagger.
# Prints the resolved local path on success for later stages to read.
#
# v1: parse + shape check only. Deeper validation (operation inventory,
# auth-scheme extraction) is left to the model reading the file directly.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: fetch_spec.sh <url-or-path>" >&2
  exit 2
fi

SRC="$1"
OUT="$(mktemp -t openapi-spec.XXXXXX)"

if [[ "$SRC" =~ ^https?:// ]]; then
  curl -fsSL "$SRC" -o "$OUT"
else
  cp "$SRC" "$OUT"
fi

python3 - "$OUT" <<'PY'
import json, sys

path = sys.argv[1]
raw = open(path, encoding="utf-8").read()

doc = None
try:
    doc = json.loads(raw)
except json.JSONDecodeError:
    try:
        import yaml  # PyYAML is a common local dep; fall back gracefully.
        doc = yaml.safe_load(raw)
    except Exception:
        print("WARN: could not parse as JSON, and PyYAML is unavailable.", file=sys.stderr)
        print("      The model should read the file directly to assess it.", file=sys.stderr)
        sys.exit(0)

if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc):
    version = doc.get("openapi") or doc.get("swagger")
    paths = doc.get("paths", {}) or {}
    print(f"OK: OpenAPI/Swagger {version}, {len(paths)} path(s).")
else:
    print("WARN: parsed, but no top-level openapi/swagger key found.", file=sys.stderr)
PY

echo "$OUT"
