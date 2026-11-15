import typing


# How deep a sampled value nests before an object or array is cut short, which also ends a recursive schema.
MAX_DEPTH = 6

# The value a string takes for each format, so a sample reads as plausible to a model.
STRING_BY_FORMAT = {
    'date-time': '2026-01-15T09:30:00Z',
    'date': '2026-01-15',
    'time': '09:30:00',
    'email': 'user@example.com',
    'uri': 'https://example.com/resource',
    'url': 'https://example.com/resource',
    'uuid': '00000000-0000-4000-8000-000000000001',
    'hostname': 'example.com',
    'ipv4': '192.0.2.1',
    'ipv6': '2001:db8::1',
    'byte': 'c2FtcGxl',
}

JSON_MEDIA_TYPES = ('application/json', 'application/vnd.github+json', '*/*')


class SchemaSampler:
    """Build one fixed example value for a JSON Schema in an OpenAPI document, following its local ``$ref``s.

    A schema's own ``example``, ``default``, or first ``enum`` value wins over anything built from its type,
    so a spec that documents its responses is quoted rather than invented.
    The same document and schema always give the same value, so a run can be repeated.
    """

    def __init__(self, document: typing.Mapping[str, typing.Any]):
        self._document = document

    def response_body(self, operation: typing.Mapping[str, typing.Any]) -> tuple[int, typing.Any]:
        """The status and JSON body an operation answers with on success, taken from its first 2xx response."""
        responses = operation.get('responses') or {}
        status = next((code for code in sorted(responses) if str(code).startswith('2')), None)
        if status is None:
            return 200, {}
        response = self.resolve(responses[status])
        content = response.get('content') or {}
        media = next((content[media_type] for media_type in JSON_MEDIA_TYPES if media_type in content), None)
        if media is None:
            media = next(
                (value for media_type, value in content.items() if 'json' in media_type),
                None,
            )
        numeric_status = int(status) if str(status).isdigit() else 200
        if media is None:
            return numeric_status, None
        if 'example' in media:
            return numeric_status, media['example']
        examples = media.get('examples') or {}
        for example in examples.values():
            resolved = self.resolve(example)
            if 'value' in resolved:
                return numeric_status, resolved['value']
        return numeric_status, self.sample(media.get('schema') or {})

    def sample(self, schema: typing.Mapping[str, typing.Any], depth: int = 0) -> typing.Any:
        """One example value for ``schema``."""
        schema = self.resolve(schema)
        for key in ('example', 'default', 'const'):
            if key in schema:
                return schema[key]
        if schema.get('enum'):
            return schema['enum'][0]
        if 'allOf' in schema:
            merged: dict[str, typing.Any] = {}
            for part in schema['allOf']:
                value = self.sample(part, depth)
                if isinstance(value, dict):
                    merged.update(value)
            return merged
        for key in ('oneOf', 'anyOf'):
            if schema.get(key):
                return self.sample(schema[key][0], depth)
        schema_type = _first_type(schema)
        if schema_type == 'object' or 'properties' in schema:
            if depth >= MAX_DEPTH:
                return {}
            properties = schema.get('properties') or {}
            return {name: self.sample(property_schema, depth + 1) for name, property_schema in properties.items()}
        if schema_type == 'array':
            if depth >= MAX_DEPTH:
                return []
            return [self.sample(schema.get('items') or {}, depth + 1)]
        if schema_type == 'string':
            return STRING_BY_FORMAT.get(schema.get('format', ''), 'string')
        if schema_type == 'integer':
            return max(int(schema.get('minimum', 1)), 1)
        if schema_type == 'number':
            return float(max(schema.get('minimum', 1), 1))
        if schema_type == 'boolean':
            return True
        return None

    def resolve(self, node: typing.Mapping[str, typing.Any]) -> typing.Mapping[str, typing.Any]:
        """Follow ``node``'s local ``$ref`` until it reaches an object that has none."""
        seen: set[str] = set()
        while isinstance(node, typing.Mapping) and isinstance(node.get('$ref'), str):
            reference = node['$ref']
            if reference in seen or not reference.startswith('#/'):
                return {}
            seen.add(reference)
            target: typing.Any = self._document
            for part in reference[2:].split('/'):
                target = target.get(part.replace('~1', '/').replace('~0', '~'), {}) if isinstance(target, dict) else {}
            node = target
        return node if isinstance(node, typing.Mapping) else {}


def _first_type(schema: typing.Mapping[str, typing.Any]) -> str | None:
    declared = schema.get('type')
    if isinstance(declared, list):
        return next((kind for kind in declared if kind != 'null'), None)
    return declared
