"""Static model capability preflight used before accepting provider-backed runs."""

from __future__ import annotations

import importlib.util
from urllib.parse import urlparse
from typing import Any

from config import Settings, normalize_media_base_url

_QWEN_TOOL_MODELS = {"qwen3.7-plus", "qwen3.7-max"}
_DASHSCOPE_INTERNATIONAL_HOST = "dashscope-intl.aliyuncs.com"
_AGENTBAY_SINGAPORE_ENDPOINT = "wuyingai.ap-southeast-1.aliyuncs.com"
_SINGAPORE_REGION = "ap-southeast-1"
_FROZEN_QWEN_IMAGE_MODEL = "qwen-image-2.0-pro-2026-06-22"
_FROZEN_WAN_VIDEO_MODEL = "wan2.7-t2v-2026-06-12"
def _is_qwen_singapore_compatible_url(url: str) -> bool:
    """Accept only the trusted Singapore Qwen Cloud fallback roots."""

    return bool(normalize_media_base_url(url))


def _make_blocker(
    *,
    code: str,
    category: str,
    service: str,
    reason: str,
    remediation: str,
) -> dict[str, str]:
    return {
        "code": code,
        "category": category,
        "service": service,
        "reason": reason,
        "remediation": remediation,
    }


def model_capability_preflight(
    settings: Settings,
    *,
    agentbay_sdk_available: bool | None = None,
    playwright_available: bool | None = None,
) -> dict[str, Any]:
    """Report whether the configured model supports Qwendom's required path.

    This check is intentionally side-effect free.  Live request success belongs
    in run metadata and the benchmark, avoiding a paid probe before every task.
    """

    qwen_base_url_host = urlparse(settings.qwen_base_url).hostname or ""
    direct_qwen_cloud = settings.provider == "qwen" and qwen_base_url_host == _DASHSCOPE_INTERNATIONAL_HOST
    supported_model = settings.provider != "qwen" or settings.qwen_model in _QWEN_TOOL_MODELS
    configured = settings.llm_enabled
    blockers: list[dict[str, str]] = []

    agentbay_sdk_ready = (
        agentbay_sdk_available
        if agentbay_sdk_available is not None
        else importlib.util.find_spec("agentbay") is not None
    )
    browser_runtime_ready = (
        playwright_available
        if playwright_available is not None
        else importlib.util.find_spec("playwright.sync_api") is not None
    )
    if not settings.agentbay_api_key:
        blockers.append(
            _make_blocker(
                code="agentbay_api_key_missing",
                category="missing_user_input",
                service="agentbay",
                reason="AgentBay API key is not configured.",
                remediation="Set AGENTBAY_API_KEY for the Singapore AgentBay account.",
            )
        )
    if not agentbay_sdk_ready:
        blockers.append(
            _make_blocker(
                code="agentbay_sdk_missing",
                category="missing_system_capability",
                service="agentbay",
                reason="The AgentBay SDK is not installed in this environment.",
                remediation="Install wuying-agentbay-sdk>=0.22.3,<0.23.0 before enabling AgentBay flows.",
            )
        )
    if settings.agentbay_endpoint != _AGENTBAY_SINGAPORE_ENDPOINT:
        blockers.append(
            _make_blocker(
                code="agentbay_endpoint_not_singapore",
                category="missing_user_input",
                service="agentbay",
                reason="AgentBay endpoint is not pinned to the Singapore control plane.",
                remediation=f"Set AGENTBAY_ENDPOINT to {_AGENTBAY_SINGAPORE_ENDPOINT}.",
            )
        )
    if settings.agentbay_region_id != _SINGAPORE_REGION:
        blockers.append(
            _make_blocker(
                code="agentbay_region_not_singapore",
                category="missing_user_input",
                service="agentbay",
                reason="AgentBay region is not pinned to Singapore.",
                remediation=f"Set AGENTBAY_REGION_ID to {_SINGAPORE_REGION}.",
            )
        )
    if settings.agentbay_image_id != "code_latest":
        blockers.append(
            _make_blocker(
                code="agentbay_image_not_supported",
                category="missing_user_input",
                service="agentbay",
                reason="AgentBay image ID is not the accepted Phase 0 image.",
                remediation="Set AGENTBAY_IMAGE_ID to code_latest.",
            )
        )

    resolved_model_studio_base_url = settings.resolved_model_studio_base_url
    if (settings.model_studio_base_url or settings.model_studio_workspace_id) and not resolved_model_studio_base_url:
        blockers.append(
            _make_blocker(
                code="model_studio_url_not_singapore",
                category="missing_user_input",
                service="model_studio",
                reason="Configured media base URL is not pinned to an approved Singapore host.",
                remediation=(
                    "Use https://dashscope-intl.aliyuncs.com/api/v1 or "
                    "https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api/v1."
                ),
            )
        )
    qwen_region_consistent = (
        settings.provider != "qwen" or _is_qwen_singapore_compatible_url(settings.qwen_base_url)
    )
    if not qwen_region_consistent:
        blockers.append(
            _make_blocker(
                code="qwen_base_url_not_singapore",
                category="missing_user_input",
                service="qwen",
                reason="Qwen base URL is not pinned to Singapore.",
                remediation="Set QWEN_BASE_URL to a Singapore DashScope-compatible endpoint.",
            )
        )
    if settings.provider == "qwen" and not supported_model:
        blockers.append(
            _make_blocker(
                code="qwen_model_not_supported",
                category="missing_system_capability",
                service="qwen",
                reason="Configured Qwen model is not in the validated structured-tool submission set.",
                remediation="Use qwen3.7-plus or qwen3.7-max for tool-capable Qwen runs.",
            )
        )

    if not settings.media_api_key:
        blockers.append(
            _make_blocker(
                code="media_api_key_missing",
                category="missing_user_input",
                service="media",
                reason="No DashScope media credential is configured.",
                remediation="Set DASHSCOPE_API_KEY or rely on the existing QWEN_API_KEY fallback.",
            )
        )
    if settings.media_api_key and not resolved_model_studio_base_url:
        blockers.append(
            _make_blocker(
                code="media_base_url_missing",
                category="missing_user_input",
                service="media",
                reason="No approved Singapore media service base URL could be resolved.",
                remediation=(
                    "Use the default DashScope intl QWEN_BASE_URL, or set MODEL_STUDIO_BASE_URL, "
                    "or set MODEL_STUDIO_WORKSPACE_ID for a Singapore workspace."
                ),
            )
        )
    if settings.qwen_image_model != _FROZEN_QWEN_IMAGE_MODEL:
        blockers.append(
            _make_blocker(
                code="qwen_image_model_unfrozen",
                category="missing_user_input",
                service="image",
                reason="Image generation model is not the accepted frozen model ID.",
                remediation=f"Set QWEN_IMAGE_MODEL to {_FROZEN_QWEN_IMAGE_MODEL}.",
            )
        )
    if settings.wan_video_model != _FROZEN_WAN_VIDEO_MODEL:
        blockers.append(
            _make_blocker(
                code="wan_video_model_unfrozen",
                category="missing_user_input",
                service="video",
                reason="Video generation model is not the accepted frozen model ID.",
                remediation=f"Set WAN_VIDEO_MODEL to {_FROZEN_WAN_VIDEO_MODEL}.",
            )
        )

    legacy_model_ready = configured and supported_model
    has_typed_blockers = bool(blockers)
    agentbay_ready = not any(blocker["service"] == "agentbay" for blocker in blockers)
    image_ready = not any(blocker["service"] in {"image", "media", "model_studio", "qwen"} for blocker in blockers)
    video_ready = not any(blocker["service"] in {"video", "media", "model_studio", "qwen"} for blocker in blockers)
    browser_ready = agentbay_ready and browser_runtime_ready

    response = {
        "ready": configured and supported_model,
        "configured": configured,
        "provider": settings.provider,
        "model": settings.active_model,
        "direct_qwen_cloud": direct_qwen_cloud,
        "structured_tools_supported": supported_model,
        "reason": (
            "ready"
            if configured and supported_model
            else "missing provider API key"
            if not configured
            else "configured Qwen model is not in the validated tool-capable submission set"
        ),
        "roadmap_ready": legacy_model_ready and not has_typed_blockers,
        "provider_services": {
            "agentbay": {
                "ready": agentbay_ready,
                "configured": bool(settings.agentbay_api_key),
                "sdk_available": agentbay_sdk_ready,
                "endpoint": settings.agentbay_endpoint,
                "region": settings.agentbay_region_id,
                "image_id": settings.agentbay_image_id,
            },
            "browser": {
                "ready": browser_ready,
                "configured": bool(settings.agentbay_api_key),
                "sdk_available": agentbay_sdk_ready,
                "playwright_available": browser_runtime_ready,
            },
            "image": {
                "ready": image_ready,
                "configured": bool(settings.media_api_key) and bool(resolved_model_studio_base_url),
                "base_url_resolved": bool(resolved_model_studio_base_url),
                "model": settings.qwen_image_model,
            },
            "video": {
                "ready": video_ready,
                "configured": bool(settings.media_api_key) and bool(resolved_model_studio_base_url),
                "base_url_resolved": bool(resolved_model_studio_base_url),
                "model": settings.wan_video_model,
            },
        },
        "typed_blockers": blockers,
    }
    return response
