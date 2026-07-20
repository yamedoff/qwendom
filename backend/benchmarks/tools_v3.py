"""Read-only public evidence tools for benchmark suite v3."""

from __future__ import annotations

from typing import Any

from benchmarks.tools_v2 import calculate


PUBLIC_RECORDS: dict[str, str] = {
    "S01": "GET /admin/export checks authentication but never checks the caller's admin role.",
    "S02": "Service tokens reject an audience value other than qwendom-api.",
    "S03": "Session cookies set Secure, HttpOnly, and SameSite=Lax.",
    "S04": "Password reset tokens are one-time values that expire after 15 minutes.",
    "S05": "Authentication attempts are limited to five per account per minute.",
    "S06": "Webhook HMAC-SHA256 signatures are compared with the normal == operator.",
    "S07": "Webhook timestamps are parsed but requests older than five minutes are not rejected.",
    "S08": "The provider event ID has a database unique constraint.",
    "S09": "The handler returns HTTP 200 before the business transaction commits.",
    "S10": "Failed webhook jobs enter a bounded dead-letter retry queue.",
    "S11": "json-path 2.1.0 has a reachable critical RCE in the request transformation path.",
    "S12": ".env.production is tracked and contains a currently valid signing credential.",
    "S13": "The package lock pins dependency versions and integrity hashes.",
    "S14": "The runtime container image is pinned by immutable digest.",
    "S15": "The release job emits a signed software bill of materials.",
    "S16": "Tests cover valid webhook signatures but omit invalid signatures, stale timestamps, authorization denial, and commit failure.",
    "S17": "The rollback drill completed in eight minutes on the release candidate.",
    "S18": "The canary deployment step takes 12 minutes.",
    "S19": "Credential containment and rotation takes 10 minutes and must finish before any deployment.",
    "S20": "Release stage order is G01, then G02, then G03, then G04.",
    "S21": "G02 includes dependency upgrade, admin authorization, constant-time comparison, replay rejection, and commit-before-ack fixes.",
    "S22": "G03 must pass invalid-signature, replay, authorization-denial, and commit-failure tests.",
    "S23": "G04 is allowed only after G01 through G03 pass and keeps the eight-minute rollback command ready.",
    "S24": "G02 fixes run in parallel and take at most 20 minutes; G01 takes 10, G03 takes 15, and G04 takes 12, so the minimum is 57 minutes.",
}

SURFACES = {
    "auth": ["S01", "S02", "S03", "S04", "S05"],
    "webhook": ["S06", "S07", "S08", "S09", "S10"],
    "supply_chain": ["S11", "S12", "S13", "S14", "S15"],
    "release_pipeline": ["S16", "S17", "S18", "S19", "S20", "S21", "S22", "S23", "S24"],
}


def _surface(name: str) -> list[dict[str, str]]:
    return [{"record_id": record_id, "text": PUBLIC_RECORDS[record_id]} for record_id in SURFACES[name]]


def inspect_auth_surface() -> list[dict[str, str]]:
    """Return authentication and authorization evidence."""
    return _surface("auth")


def inspect_webhook_surface() -> list[dict[str, str]]:
    """Return webhook integrity and processing evidence."""
    return _surface("webhook")


def inspect_supply_chain_surface() -> list[dict[str, str]]:
    """Return dependency, secret, and build provenance evidence."""
    return _surface("supply_chain")


def inspect_release_pipeline_surface() -> list[dict[str, str]]:
    """Return test, ordering, timing, deployment, and rollback evidence."""
    return _surface("release_pipeline")


def lookup_security_record(record_id: str) -> dict[str, Any]:
    """Return one record or a self-correcting unknown-ID response."""
    if record_id not in PUBLIC_RECORDS:
        return {"record_id": record_id, "found": False, "valid_record_ids": sorted(PUBLIC_RECORDS)}
    return {"record_id": record_id, "found": True, "text": PUBLIC_RECORDS[record_id]}


SHARED_TOOLS = [
    inspect_auth_surface, inspect_webhook_surface, inspect_supply_chain_surface,
    inspect_release_pipeline_surface, lookup_security_record, calculate,
]
TOOL_NAMES = tuple(tool.__name__ for tool in SHARED_TOOLS)
