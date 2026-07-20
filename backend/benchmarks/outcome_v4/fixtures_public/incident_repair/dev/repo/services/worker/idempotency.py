processed_ids: set[str] = set()


def process_job(job_id: str) -> str:
    """Buggy worker that re-processes duplicate jobs."""

    processed_ids.add(job_id)
    return "processed"
