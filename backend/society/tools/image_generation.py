from __future__ import annotations

import time
from typing import Any, Callable, Mapping

import httpx
from agno.tools import Toolkit

from config import Settings
from .media_store import MediaArtifactStore, validate_model_studio_base_url


IMAGE_MODEL_ID = "qwen-image-2.0-pro-2026-06-22"
MAX_PROMPT_LENGTH = 4000
MAX_NEGATIVE_PROMPT_LENGTH = 2000
MIN_DIMENSION = 512
MAX_DIMENSION = 2048
MIN_PIXELS = 512 * 512
MAX_PIXELS = 2048 * 2048
MAX_IMAGE_COUNT = 6
MAX_SEED = 2_147_483_647
DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS = 180.0
DEFAULT_IMAGE_REQUEST_MAX_ATTEMPTS = 2
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429})


class ImageGenerationTools(Toolkit):
    """Agno toolkit for durable image generation through the Model Studio REST API."""

    def __init__(
        self,
        *,
        settings: Settings,
        artifact_store: MediaArtifactStore,
        http_client: httpx.Client,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        request_timeout_seconds: float = DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS,
        request_max_attempts: int = DEFAULT_IMAGE_REQUEST_MAX_ATTEMPTS,
    ) -> None:
        super().__init__(name="image_generation_tools", auto_register=False)
        self._settings = settings
        self._artifact_store = artifact_store
        self._http_client = http_client
        self._event_sink = event_sink
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._request_max_attempts = int(request_max_attempts)
        if self._request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self._request_max_attempts < 1:
            raise ValueError("request_max_attempts must be at least one")
        for name in ("generate_images", "inspect_image", "publish_image"):
            self.register(getattr(self, name))

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """Return whether repeating a real generation request can recover safely."""

        return status_code in _RETRYABLE_HTTP_STATUS_CODES or status_code >= 500

    def _retry_delay_seconds(self, attempt: int) -> float:
        """Compute a bounded delay after a failed one-based attempt."""

        base = max(0.0, float(self._settings.provider_backoff_base_seconds))
        cap = max(0.0, float(self._settings.provider_backoff_cap_seconds))
        return min(cap, base * (2 ** max(0, attempt - 1)))

    def _request_generation_payload(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        request_body: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
        """Call Qwen Image with bounded retries and return a stable error envelope.

        A timeout or transient provider/transport failure gets one bounded retry by
        default. Permanent client errors (including authentication and request
        validation failures) are returned immediately so a bad request is never
        amplified into repeated provider calls.
        """

        for attempt in range(1, self._request_max_attempts + 1):
            try:
                response = self._http_client.post(
                    url,
                    headers=headers,
                    json=request_body,
                    timeout=self._request_timeout_seconds,
                )
                response.raise_for_status()
                return response.json(), None, None
            except httpx.TimeoutException:
                error_code = "network_image_timeout"
                error_message = "Image request timed out"
                retryable = True
            except httpx.HTTPStatusError as exc:
                error_code = "provider_image_http"
                error_message = str(exc)
                retryable = self._is_retryable_status(exc.response.status_code)
            except httpx.RequestError as exc:
                error_code = "provider_image_http"
                error_message = str(exc)
                retryable = True
            except ValueError as exc:
                return None, "provider_image_json", str(exc)

            if not retryable or attempt >= self._request_max_attempts:
                return None, error_code, error_message
            self._sleeper(self._retry_delay_seconds(attempt))

        raise AssertionError("bounded image request loop exited unexpectedly")

    def _success(self, *, data: Mapping[str, Any], error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
        payload = {
            "success": error_code is None,
            "data": self._artifact_store.redact_value(dict(data)),
            "error_code": error_code,
            "error_message": self._artifact_store.redact_value(error_message) if error_message else None,
        }
        if self._event_sink is not None:
            self._event_sink(self._artifact_store.redact_value(payload))
        return payload

    def _validate_config(self) -> str | None:
        if not self._settings.media_api_key:
            return "Missing DASHSCOPE_API_KEY or QWEN_API_KEY"
        if self._settings.qwen_image_model != IMAGE_MODEL_ID:
            return "Configured image model does not match the frozen Phase 3 model"
        try:
            validate_model_studio_base_url(self._settings.resolved_model_studio_base_url)
        except ValueError:
            return "Media service base URL configuration is invalid"
        return None

    def _validate_prompt(self, value: str, *, allow_empty: bool, field_name: str, limit: int) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        trimmed = value.strip()
        if not trimmed and not allow_empty:
            raise ValueError(f"{field_name} must not be empty")
        if len(trimmed) > limit:
            raise ValueError(f"{field_name} exceeds maximum length")
        return trimmed

    def generate_images(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = 1024,
        height: int = 1024,
        count: int = 1,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Generate one or more images and immediately download them into the local media store."""

        config_error = self._validate_config()
        if config_error is not None:
            return self._success(data={"artifact_ids": []}, error_code="config_image_unavailable", error_message=config_error)
        try:
            validated_prompt = self._validate_prompt(prompt, allow_empty=False, field_name="prompt", limit=MAX_PROMPT_LENGTH)
            validated_negative = self._validate_prompt(
                negative_prompt,
                allow_empty=True,
                field_name="negative_prompt",
                limit=MAX_NEGATIVE_PROMPT_LENGTH,
            )
            width = int(width)
            height = int(height)
            count = int(count)
            if width < MIN_DIMENSION or width > MAX_DIMENSION:
                raise ValueError("width must be between 512 and 2048")
            if height < MIN_DIMENSION or height > MAX_DIMENSION:
                raise ValueError("height must be between 512 and 2048")
            pixels = width * height
            if pixels < MIN_PIXELS or pixels > MAX_PIXELS:
                raise ValueError("image size exceeds the supported pixel bounds")
            if count < 1 or count > MAX_IMAGE_COUNT:
                raise ValueError("count must be between 1 and 6")
            if seed is not None and (int(seed) < 0 or int(seed) > MAX_SEED):
                raise ValueError("seed must be between 0 and 2147483647")
        except Exception as exc:
            return self._success(data={"artifact_ids": []}, error_code="validation_image_request", error_message=str(exc))

        started = self._clock()
        request_body = {
            "model": IMAGE_MODEL_ID,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": validated_prompt}],
                    }
                ]
            },
            "parameters": {
                "negative_prompt": validated_negative,
                "prompt_extend": True,
                "watermark": False,
                "size": f"{width}*{height}",
                "n": count,
            },
        }
        if seed is not None:
            request_body["parameters"]["seed"] = int(seed)
        base_url = validate_model_studio_base_url(self._settings.resolved_model_studio_base_url)
        payload, provider_error_code, provider_error_message = self._request_generation_payload(
            url=f"{base_url}/services/aigc/multimodal-generation/generation",
            headers={
                "Authorization": f"Bearer {self._settings.media_api_key}",
                "Content-Type": "application/json",
            },
            request_body=request_body,
        )
        if provider_error_code is not None:
            return self._success(
                data={"artifact_ids": []},
                error_code=provider_error_code,
                error_message=provider_error_message,
            )
        assert payload is not None

        request_id = str(payload.get("request_id") or "")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        artifact_ids: list[str] = []
        artifacts: list[dict[str, Any]] = []
        error_message: str | None = None
        try:
            choices = payload.get("output", {}).get("choices", [])
            urls: list[str] = []
            for choice in choices:
                content = choice.get("message", {}).get("content", [])
                for item in content:
                    image_url = item.get("image")
                    if image_url:
                        urls.append(str(image_url))
            if len(urls) < count:
                raise ValueError("Provider returned fewer images than requested")
            for image_url in urls[:count]:
                downloaded = self._artifact_store.save_downloaded_artifact(
                    kind="image",
                    model_id=IMAGE_MODEL_ID,
                    prompt=validated_prompt,
                    negative_prompt=validated_negative,
                    request_id=request_id,
                    latency_ms=int((self._clock() - started) * 1000),
                    client=self._http_client,
                    download_url=image_url,
                    usage=usage,
                    cost=None,
                    mime_prefix="image/",
                    width=usage.get("width", width),
                    height=usage.get("height", height),
                    seed=seed,
                )
                artifact_ids.append(downloaded.artifact_id)
                artifacts.append(downloaded.manifest)
        except Exception as exc:
            error_message = str(exc)

        result_data = {
            "artifact_ids": artifact_ids,
            "artifacts": artifacts,
            "request_id": request_id,
            "usage": usage,
        }
        if error_message is not None:
            return self._success(data=result_data, error_code="download_image_partial", error_message=error_message)
        return self._success(data=result_data)

    def inspect_image(self, artifact_id: str) -> dict[str, Any]:
        """Return the public manifest for a previously downloaded image artifact."""

        try:
            manifest = self._artifact_store.load_artifact_manifest(artifact_id)
        except Exception as exc:
            return self._success(data={"artifact_id": artifact_id}, error_code="state_image_not_found", error_message=str(exc))
        return self._success(data={"artifact": manifest})

    def publish_image(self, artifact_id: str, purpose: str) -> dict[str, Any]:
        """Mark an image artifact as published without storing the raw purpose text."""

        try:
            manifest = self._artifact_store.mark_artifact_published(artifact_id, purpose)
        except Exception as exc:
            return self._success(data={"artifact_id": artifact_id}, error_code="state_image_publish_failed", error_message=str(exc))
        return self._success(data={"artifact": manifest})
