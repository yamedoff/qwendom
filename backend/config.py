from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["qwen", "qwen_legacy", "qwen_legacy"]

_DASHSCOPE_INTL_HOST = "dashscope-intl.aliyuncs.com"
_SINGAPORE_WORKSPACE_SUFFIX = ".ap-southeast-1.maas.aliyuncs.com"


def _is_valid_workspace_label(value: str) -> bool:
    if not value or len(value) > 63:
        return False
    if not value[0].isalnum() or not value[-1].isalnum():
        return False
    return all(ch.isalnum() or ch == "-" for ch in value)


def normalize_media_base_url(base_url: str, *, allow_dashscope_intl: bool = True) -> str:
    """Return a normalized Singapore media service base URL or an empty string.

    Accepted hosts are intentionally strict:
    - ``dashscope-intl.aliyuncs.com`` with ``/compatible-mode/v1`` or ``/api/v1``
    - ``{workspace}.ap-southeast-1.maas.aliyuncs.com`` with an empty path or ``/api/v1``

    Any userinfo, query, fragment, non-HTTPS scheme, non-default port, wrong
    region, deceptive suffix, or unexpected path shape is rejected.
    """

    if not isinstance(base_url, str) or not base_url.strip():
        return ""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme.lower() != "https":
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    if parsed.port not in (None, 443):
        return ""

    hostname = parsed.hostname or ""
    normalized_path = parsed.path.rstrip("/")
    if hostname == _DASHSCOPE_INTL_HOST:
        if not allow_dashscope_intl:
            return ""
        if normalized_path not in {"", "/api/v1", "/compatible-mode/v1"}:
            return ""
        return f"https://{hostname}/api/v1"

    if not hostname.endswith(_SINGAPORE_WORKSPACE_SUFFIX):
        return ""
    workspace_label = hostname[: -len(_SINGAPORE_WORKSPACE_SUFFIX)]
    if "." in workspace_label or not _is_valid_workspace_label(workspace_label):
        return ""
    if normalized_path not in {"", "/api/v1"}:
        return ""
    return f"https://{hostname}/api/v1"


class Settings(BaseSettings):
    """Runtime configuration for local demos and cloud deployments."""

    provider: Provider = Field(default="qwen", alias="LLM_PROVIDER")

    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )
    # Qwen 3.7 Plus is the selected Agent Society runtime and supports the
    # native structured tool path through the DashScope-compatible client.
    qwen_model: str = Field(default="qwen3.7-plus", alias="QWEN_MODEL")
    # Optional dedicated DashScope/Model Studio key for media services. When
    # omitted, the preflight falls back to QWEN_API_KEY because this app uses
    # the same Singapore DashScope account for text and media.
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    # AgentBay credentials and region are kept explicit so the backend can
    # report static blockers before attempting any paid sandbox call.
    agentbay_api_key: str = Field(default="", alias="AGENTBAY_API_KEY")
    agentbay_endpoint: str = Field(
        default="wuyingai.ap-southeast-1.aliyuncs.com",
        alias="AGENTBAY_ENDPOINT",
    )
    agentbay_region_id: str = Field(default="ap-southeast-1", alias="AGENTBAY_REGION_ID")
    agentbay_image_id: str = Field(default="code_latest", alias="AGENTBAY_IMAGE_ID")
    agentbay_browser_image_id: str = Field(default="browser_latest", alias="AGENTBAY_BROWSER_IMAGE_ID")
    # The app owns the optional Singapore workspace selection. When unset, media
    # calls can still fall back to the existing Qwen Cloud DashScope account.
    model_studio_workspace_id: str = Field(default="", alias="MODEL_STUDIO_WORKSPACE_ID")
    # Legacy name retained for compatibility. The value now represents the
    # resolved media service base and may be a workspace MAAS host or the
    # Singapore DashScope fallback root.
    model_studio_base_url: str = Field(default="", alias="MODEL_STUDIO_BASE_URL")
    # Frozen model IDs are enforced for reproducibility during Phase 0.
    qwen_image_model: str = Field(
        default="qwen-image-2.0-pro-2026-06-22",
        alias="QWEN_IMAGE_MODEL",
    )
    wan_video_model: str = Field(
        default="wan2.7-t2v-2026-06-12",
        alias="WAN_VIDEO_MODEL",
    )

    qwen_legacy_api_key: str = Field(default="", alias="QWEN_LEGACY_API_KEY")
    qwen_legacy_base_url: str = Field(
        default="https://qwen_legacy.ai/api/v1",
        alias="QWEN_LEGACY_BASE_URL",
    )
    qwen_legacy_model: str = Field(default="qwen3.7-plus", alias="QWEN_LEGACY_MODEL")

    qwen_legacy_api_key: str = Field(default="", alias="QWEN_LEGACY_API_KEY")
    qwen_legacy_model: str = Field(default="gemma-4-31b", alias="QWEN_LEGACY_MODEL")

    llm_timeout_seconds: int = Field(default=60, alias="LLM_TIMEOUT_SECONDS")
    provider_max_attempts: int = Field(default=3, alias="PROVIDER_MAX_ATTEMPTS")
    provider_backoff_base_seconds: float = Field(default=1.0, alias="PROVIDER_BACKOFF_BASE_SECONDS")
    provider_backoff_cap_seconds: float = Field(default=30.0, alias="PROVIDER_BACKOFF_CAP_SECONDS")
    allow_deterministic_no_key: bool = Field(default=False, alias="ALLOW_DETERMINISTIC_NO_KEY")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    frontend_dist_dir: str = Field(default="", alias="FRONTEND_DIST_DIR")
    knowledge_dir: str = Field(default="backend/society/knowledge/data", alias="KNOWLEDGE_DIR")
    agno_db_url: str = Field(default="", alias="AGNO_DB_URL")
    agno_sqlite_file: str = Field(default="backend/society/data/agno.sqlite", alias="AGNO_SQLITE_FILE")
    event_store_file: str = Field(default="", alias="EVENT_STORE_FILE")
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    artifact_tracking_enabled: bool = Field(default=True, alias="ARTIFACT_TRACKING_ENABLED")
    delegation_tools_enabled: bool = Field(default=True, alias="DELEGATION_TOOLS_ENABLED")
    # Qwen demo runs must use the Agno-native tool paths. The legacy paths
    # synthesize debate opinions and route voting through a weaker schema that
    # has proven unreliable with qwen3.6-flash.
    native_debate_enabled: bool = Field(default=True, alias="NATIVE_DEBATE_ENABLED")
    native_voting_enabled: bool = Field(default=True, alias="NATIVE_VOTING_ENABLED")
    workflow_routing_enabled: bool = Field(default=True, alias="WORKFLOW_ROUTING_ENABLED")
    evaluation_metrics_enabled: bool = Field(default=True, alias="EVALUATION_METRICS_ENABLED")
    pre_execution_conversation_enabled: bool = Field(default=True, alias="PRE_EXECUTION_CONVERSATION_ENABLED")
    readiness_voting_enabled: bool = Field(default=True, alias="READINESS_VOTING_ENABLED")
    agent_profiles_enabled: bool = Field(default=True, alias="AGENT_PROFILES_ENABLED")
    social_tools_enabled: bool = Field(default=True, alias="SOCIAL_TOOLS_ENABLED")
    social_trace_enabled: bool = Field(default=True, alias="SOCIAL_TRACE_ENABLED")
    contextual_trust_enabled: bool = Field(default=True, alias="CONTEXTUAL_TRUST_ENABLED")
    role_specific_tools_enabled: bool = Field(default=True, alias="ROLE_SPECIFIC_TOOLS_ENABLED")
    context7_mcp_enabled: bool = Field(default=True, alias="CONTEXT7_MCP_ENABLED")
    context7_mcp_command: str = Field(default="npx -y @upstash/context7-mcp", alias="CONTEXT7_MCP_COMMAND")
    context7_max_calls_per_turn: int = Field(default=2, alias="CONTEXT7_MAX_CALLS_PER_TURN")
    readiness_concurrency: int = Field(default=3, alias="READINESS_CONCURRENCY")
    efficient_society_enabled: bool = Field(default=True, alias="EFFICIENT_SOCIETY_ENABLED")
    benchmark_suite_tools_enabled: bool = Field(default=False, alias="BENCHMARK_SUITE_TOOLS_ENABLED")
    benchmark_suite_version: str = Field(default="v2", alias="BENCHMARK_SUITE_VERSION")
    # New product runs use the fixed-specialist executor.  The flag remains
    # available only to gate explicitly requested legacy-composer replay; it
    # must not silently redirect a fixed-specialist mission to demo delegation.
    team_composition_execution_enabled: bool = Field(default=True, alias="TEAM_COMPOSITION_EXECUTION_ENABLED")
    # New composition runs use the elected leader and immutable repository
    # templates. The legacy composer remains available only for replaying and
    # testing historical plans while the migration is completed.
    team_composition_strategy: Literal["fixed_specialists", "legacy_composer"] = Field(
        default="fixed_specialists",
        alias="TEAM_COMPOSITION_STRATEGY",
    )
    subtask_max_attempts: int = Field(default=3, alias="SUBTASK_MAX_ATTEMPTS")
    subtask_backoff_base_seconds: float = Field(default=1.0, alias="SUBTASK_BACKOFF_BASE_SECONDS")
    subtask_backoff_cap_seconds: float = Field(default=30.0, alias="SUBTASK_BACKOFF_CAP_SECONDS")
    society_max_model_workers: int = Field(default=4, alias="SOCIETY_MAX_MODEL_WORKERS")
    society_max_agentbay_sessions: int = Field(default=3, alias="SOCIETY_MAX_AGENTBAY_SESSIONS")
    society_max_media_jobs: int = Field(default=2, alias="SOCIETY_MAX_MEDIA_JOBS")
    society_max_dynamic_specialists: int = Field(default=5, alias="SOCIETY_MAX_DYNAMIC_SPECIALISTS")
    agentbay_session_timeout_seconds: int = Field(default=1800, alias="AGENTBAY_SESSION_TIMEOUT_SECONDS")
    video_job_timeout_seconds: int = Field(default=600, alias="VIDEO_JOB_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def active_api_key(self) -> str:
        if self.provider == "qwen_legacy":
            return self.qwen_legacy_api_key
        return self.qwen_legacy_api_key if self.provider == "qwen_legacy" else self.qwen_api_key

    @property
    def active_base_url(self) -> str:
        if self.provider == "qwen_legacy":
            return ""
        return self.qwen_legacy_base_url if self.provider == "qwen_legacy" else self.qwen_base_url

    @property
    def active_model(self) -> str:
        if self.provider == "qwen_legacy":
            return self.qwen_legacy_model
        return self.qwen_legacy_model if self.provider == "qwen_legacy" else self.qwen_model

    @property
    def llm_enabled(self) -> bool:
        return bool(self.active_api_key)

    @property
    def media_api_key(self) -> str:
        """Return the preferred media credential without exposing it in dumps."""

        return self.dashscope_api_key or self.qwen_api_key

    @property
    def resolved_model_studio_base_url(self) -> str:
        """Resolve the media service base safely under the legacy property name."""

        if self.model_studio_base_url:
            return normalize_media_base_url(self.model_studio_base_url)
        if self.model_studio_workspace_id:
            return normalize_media_base_url(
                f"https://{self.model_studio_workspace_id}.ap-southeast-1.maas.aliyuncs.com",
                allow_dashscope_intl=False,
            )
        return normalize_media_base_url(self.qwen_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
