import pydantic


class McpRecordOrigin(pydantic.BaseModel):
    """Which server wrote an MCP record, and against which upstream authorization server.

    Every code, token and sign-in carries one.
    One store serves every server in the gateway and outlives configuration changes.
    A record read where the origin differs, or that carries none, is treated as absent.
    Without that, a token minted for one server would unlock the upstream credential of the next,
    which is the cross-server credential reuse the MCP spec forbids.
    ``upstream_authorization_server`` is ``None`` where no upstream sign-in stands behind the records.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    issuer: str
    upstream_authorization_server: str | None = None
