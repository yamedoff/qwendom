import os


def database_url() -> str:
    """Return the buggy database URL setting."""

    return os.getenv("DATABASE_URL", "postgresql://localhost/devdb")
