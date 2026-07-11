from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["qwen", "qwen_legacy", "qwen_legacy"]


class Settings(BaseSettings):
    """Runtime configuration for local demos and cloud deployments."""

    provider: Provider = Field(default="qwen_legacy", alias="LLM_PROVIDER")

    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )
    qwen_model: str = Field(default="qwen-plus", alias="QWEN_MODEL")

    qwen_legacy_api_key: str = Field(default="", alias="QWEN_LEGACY_API_KEY")
    qwen_legacy_base_url: str = Field(
        default="https://qwen_legacy.ai/api/v1",
        alias="QWEN_LEGACY_BASE_URL",
    )
    qwen_legacy_model: str = Field(default="qwen3.7-plus", alias="QWEN_LEGACY_MODEL")

    qwen_legacy_api_key: str = Field(default="", alias="QWEN_LEGACY_API_KEY")
    qwen_legacy_model: str = Field(default="gemma-4-31b", alias="QWEN_LEGACY_MODEL")

    llm_timeout_seconds: int = Field(default=60, alias="LLM_TIMEOUT_SECONDS")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    knowledge_dir: str = Field(default="backend/society/knowledge/data", alias="KNOWLEDGE_DIR")
    agno_db_url: str = Field(default="", alias="AGNO_DB_URL")
    agno_sqlite_file: str = Field(default="backend/society/data/agno.sqlite", alias="AGNO_SQLITE_FILE")
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    artifact_tracking_enabled: bool = Field(default=True, alias="ARTIFACT_TRACKING_ENABLED")
    delegation_tools_enabled: bool = Field(default=True, alias="DELEGATION_TOOLS_ENABLED")
    native_debate_enabled: bool = Field(default=False, alias="NATIVE_DEBATE_ENABLED")
    native_voting_enabled: bool = Field(default=False, alias="NATIVE_VOTING_ENABLED")
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
    readiness_concurrency: int = Field(default=3, alias="READINESS_CONCURRENCY")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
