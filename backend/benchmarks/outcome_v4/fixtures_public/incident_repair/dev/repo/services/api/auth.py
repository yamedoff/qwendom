def allow_support_scope(scope: str, token_enabled: bool) -> bool:
    """Buggy authorization check used by the development fixture."""

    return token_enabled and scope == "admin"
