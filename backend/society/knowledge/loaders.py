from __future__ import annotations

from pathlib import Path

from agno.knowledge import FileSystemKnowledge


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _resolve_knowledge_dir(knowledge_dir: str) -> Path:
    base = Path(knowledge_dir)
    if base.is_absolute():
        return base
    repo_candidate = _REPO_ROOT / base
    if repo_candidate.exists():
        return repo_candidate
    cwd_candidate = Path.cwd() / base
    if cwd_candidate.exists():
        return cwd_candidate
    return base.resolve()


def role_knowledge_path(knowledge_dir: str, role: str) -> Path:
    """Return the directory used for a role-scoped knowledge office."""

    slug = role.lower().replace(" ", "-")
    return _resolve_knowledge_dir(knowledge_dir) / slug


def load_role_knowledge(role: str, knowledge_dir: str) -> FileSystemKnowledge | None:
    """Load a role-scoped Agno knowledge base when local files exist.

    Missing knowledge folders are treated as an empty office so local demos do
    not fail at startup. Creating markdown or text files under the returned
    role path opt-ins that role to Agno filesystem knowledge retrieval.
    """

    path = role_knowledge_path(knowledge_dir, role)
    if not path.exists():
        path = role_knowledge_path(knowledge_dir, _fallback_office_role(role))
    if not path.exists() or not any(path.iterdir()):
        return None
    return FileSystemKnowledge(
        base_dir=str(path),
        include_patterns=["*.md", "*.txt", "*.json"],
        exclude_patterns=[".*", "__pycache__/*"],
    )


def _fallback_office_role(role: str) -> str:
    """Map dynamic child specialist roles back to persistent office knowledge."""

    lowered = role.lower()
    if "research" in lowered or "evidence" in lowered:
        return "Research Analyst"
    if "risk" in lowered or "critic" in lowered or "review" in lowered:
        return "Adversarial Reviewer"
    if "implement" in lowered or "build" in lowered or "engineer" in lowered or "scope" in lowered:
        return "Implementation Engineer"
    return "Systems Architect"
