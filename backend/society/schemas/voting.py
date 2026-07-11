from __future__ import annotations

from pydantic import BaseModel


class TallyResult(BaseModel):
    """Result of tallying native ballots."""

    tally: dict[str, int]
    winner: str | None = None
