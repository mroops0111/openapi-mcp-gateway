import pydantic


class McpRecordOrigin(pydantic.BaseModel):
    """Which server wrote an MCP record, and against which authorization server its sign-ins continue.

    Every code, token and sign-in carries one.
    One store serves every server in the gateway and outlives configuration changes.
    A record read where the origin differs, or that carries none, is treated as absent.
    Without that, a token minted for one server would unlock the upstream credential of the next,
    which is the cross-server credential reuse the MCP spec forbids.
    ``next_sign_in_server`` identifies where the sign-in after consent happens, as ``McpNextSignIn`` describes it,
    and is ``None`` where approval ends the sign-in.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    issuer: str
    next_sign_in_server: str | None = None
