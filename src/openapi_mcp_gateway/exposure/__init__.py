"""Strategies for exposing OpenAPI operations as MCP primitives.

Organised by MCP primitive type:

- :mod:`.tool` covers the ``tool`` primitive,
  with both static (:class:`ToolGenerator`) and dynamic (:class:`MetaToolGenerator`) exposure.
- :mod:`.resource` covers the ``resource`` primitive (:class:`ResourceGenerator`, static only today).

Cross-primitive helpers live in :mod:`._shared` (sanitising, type mapping, override-aware derives)
and :mod:`._upstream` (the async callable that issues one upstream HTTP request per invocation).
"""

from ._shared import (
    UpstreamBinding,
    build_input_schema,
    derive_description,
    derive_name,
)
from .resource import (
    ResourceGenerator,
    build_resource_read_function,
    derive_resource_mime_type,
    derive_resource_uri,
)
from .tool import (
    MetaToolGenerator,
    ToolGenerator,
    build_tool_function,
    derive_tool_annotations,
    derive_tool_title,
)


__all__ = [
    'MetaToolGenerator',
    'ResourceGenerator',
    'ToolGenerator',
    'UpstreamBinding',
    'build_input_schema',
    'build_resource_read_function',
    'build_tool_function',
    'derive_description',
    'derive_name',
    'derive_resource_mime_type',
    'derive_resource_uri',
    'derive_tool_annotations',
    'derive_tool_title',
]
