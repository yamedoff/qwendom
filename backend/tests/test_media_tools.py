from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import Settings
from society.tools.image_generation import (
    DEFAULT_IMAGE_REQUEST_MAX_ATTEMPTS,
    DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS,
    IMAGE_MODEL_ID,
    ImageGenerationTools,
)
from society.tools.media_store import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DEFAULT_DOWNLOAD_MAX_ATTEMPTS,
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    ARTIFACT_ID_PATTERN,
    JOB_ID_PATTERN,
    MediaArtifactStore,
    StaleJobVersionError,
    validate_model_studio_base_url,
    validate_provider_media_url,
)
from society.tools.video_generation import VIDEO_MODEL_ID, VideoGenerationTools


IMAGE_URL_1 = "https://dashscope-result-1.aliyuncs.com/image-1.png"
IMAGE_URL_2 = "https://dashscope-result-1.aliyuncs.com/image-2.png"
VIDEO_URL = "https://dashscope-result-1.aliyuncs.com/video.mp4"


class Clock:
    def __init__(self, start: datetime):
        self.current = start

    def now(self) -> datetime:
        return self.current

    def timestamp(self) -> float:
        return self.current.timestamp()

    def monotonic(self) -> float:
        return self.current.timestamp()

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def make_settings(**overrides: Any) -> Settings:
    data = {
        "DASHSCOPE_API_KEY": "dash-key-secret",
        "QWEN_API_KEY": "qwen-key-secret",
        "QWEN_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "MODEL_STUDIO_WORKSPACE_ID": "workspace-secret",
        "QWEN_IMAGE_MODEL": IMAGE_MODEL_ID,
        "WAN_VIDEO_MODEL": VIDEO_MODEL_ID,
        "PROVIDER_BACKOFF_BASE_SECONDS": 2.0,
        "PROVIDER_BACKOFF_CAP_SECONDS": 30.0,
    }
    data.update(overrides)
    return Settings(**data)


def make_store(
    tmp_path: Path,
    clock: Clock,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    directory_fsync: Any | None = None,
    **overrides: Any,
) -> MediaArtifactStore:
    return MediaArtifactStore(
        tmp_path / "media-store",
        clock=clock.now,
        max_download_bytes=max_download_bytes,
        directory_fsync=directory_fsync,
        **overrides,
    )


def test_registered_public_tools_exact(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    store = make_store(tmp_path, clock)

    image_tools = ImageGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.monotonic)
    video_tools = VideoGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.timestamp)

    assert sorted(image_tools.functions) == ["generate_images", "inspect_image", "publish_image"]
    assert sorted(video_tools.functions) == [
        "cancel_video_job",
        "collect_video",
        "get_video_job",
        "inspect_video",
        "submit_text_to_video",
    ]


def test_model_studio_base_url_validation_and_no_http_on_invalid_override(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = make_store(tmp_path, clock)

    assert validate_model_studio_base_url("https://workspace-secret.ap-southeast-1.maas.aliyuncs.com") == (
        "https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1"
    )
    assert validate_model_studio_base_url("https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1") == (
        "https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1"
    )
    assert validate_model_studio_base_url("https://dashscope-intl.aliyuncs.com/compatible-mode/v1") == (
        "https://dashscope-intl.aliyuncs.com/api/v1"
    )
    assert validate_model_studio_base_url("https://dashscope-intl.aliyuncs.com/api/v1") == (
        "https://dashscope-intl.aliyuncs.com/api/v1"
    )

    image_tools = ImageGenerationTools(
        settings=make_settings(MODEL_STUDIO_BASE_URL="http://evil.example/api/v1"),
        artifact_store=store,
        http_client=client,
        clock=clock.monotonic,
    )
    video_tools = VideoGenerationTools(
        settings=make_settings(MODEL_STUDIO_BASE_URL="https://user:pass@workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1"),
        artifact_store=store,
        http_client=client,
        clock=clock.timestamp,
    )

    image_result = image_tools.generate_images("prompt")
    video_result = video_tools.submit_text_to_video("prompt")
    assert image_result["error_code"] == "config_image_unavailable"
    assert video_result["error_code"] == "config_video_unavailable"
    assert "dash-key-secret" not in json.dumps(image_result)
    assert "dash-key-secret" not in json.dumps(video_result)
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://dashscope-intl.aliyuncs.com.evil.com/api/v1",
        "https://dashscope-intl.aliyuncs.com/?x=1",
        "https://dashscope-intl.aliyuncs.com/api/v1#frag",
        "https://user:pass@dashscope-intl.aliyuncs.com/api/v1",
        "https://dashscope-intl.aliyuncs.com:444/api/v1",
        "https://workspace-secret.us-west-1.maas.aliyuncs.com/api/v1",
        "https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        "https://nested.workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1",
    ],
)
def test_model_studio_base_url_rejects_untrusted_or_unexpected_shapes(url):
    with pytest.raises(ValueError):
        validate_model_studio_base_url(url)


def test_media_base_resolution_precedence_and_qwen_fallback(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    explicit_settings = make_settings(
        MODEL_STUDIO_BASE_URL="https://dashscope-intl.aliyuncs.com/api/v1",
        MODEL_STUDIO_WORKSPACE_ID="workspace-secret",
        QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    workspace_settings = make_settings(
        MODEL_STUDIO_BASE_URL="",
        MODEL_STUDIO_WORKSPACE_ID="workspace-secret",
        QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    fallback_settings = make_settings(
        DASHSCOPE_API_KEY="",
        MODEL_STUDIO_BASE_URL="",
        MODEL_STUDIO_WORKSPACE_ID="",
        QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )

    assert explicit_settings.resolved_model_studio_base_url == "https://dashscope-intl.aliyuncs.com/api/v1"
    assert workspace_settings.resolved_model_studio_base_url == (
        "https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1"
    )
    assert fallback_settings.resolved_model_studio_base_url == "https://dashscope-intl.aliyuncs.com/api/v1"
    assert fallback_settings.media_api_key == "qwen-key-secret"

    image_tools = ImageGenerationTools(
        settings=fallback_settings,
        artifact_store=store,
        http_client=client,
        clock=clock.monotonic,
    )
    assert image_tools._validate_config() is None


def test_strict_id_validation_and_directory_containment(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    store = make_store(tmp_path, clock)

    assert ARTIFACT_ID_PATTERN.fullmatch(store.new_artifact_id())
    assert JOB_ID_PATTERN.fullmatch(store.new_job_id())

    with pytest.raises(ValueError):
        store.load_artifact_manifest("../artifact_x")
    with pytest.raises(ValueError):
        store.load_video_job("../job_x")
    with pytest.raises(ValueError):
        store._contain_in(store.root_dir / "manifests", store.root_dir / "private_jobs" / "job_x.json")


@pytest.mark.parametrize(
    "url",
    [
        "http://dashscope-result-1.aliyuncs.com/x.png",
        "https://localhost/x.png",
        "https://169.254.169.254/x.png",
        "https://10.0.0.7/x.png",
        "https://dashscope-result-1.aliyuncs.com.evil.com/x.png",
        "https://user:pass@dashscope-result-1.aliyuncs.com/x.png",
        "https://dashscope-result-1.aliyuncs.com:444/x.png",
        "https://127.0.0.1/x.png",
        "https://[::1]/x.png",
        "https://dashscope-result-1.aliyuncs.com/x.png#frag",
    ],
)
def test_provider_media_url_ssrf_rejections_make_zero_outbound_request(tmp_path, url):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = make_store(tmp_path, clock)

    with pytest.raises(ValueError):
        validate_provider_media_url(url)
    with pytest.raises(ValueError):
        store.save_downloaded_artifact(
            kind="image",
            model_id=IMAGE_MODEL_ID,
            prompt="prompt",
            negative_prompt="",
            request_id="req",
            latency_ms=1,
            client=client,
            download_url=url,
            usage={},
            cost=None,
            mime_prefix="image/",
        )
    assert calls == []


def test_provider_media_url_allows_official_hosts_and_rejects_redirects(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://dashscope-result-1.aliyuncs.com/redirect.png":
            return httpx.Response(302, headers={"location": "https://evil.example/pwn.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = make_store(tmp_path, clock)

    assert validate_provider_media_url("https://dashscope-result-1.aliyuncs.com/ok.png") == (
        "https://dashscope-result-1.aliyuncs.com/ok.png"
    )
    assert validate_provider_media_url("https://media-oss-cn.aliyuncs.com/ok.png?sig=1").startswith(
        "https://media-oss-cn.aliyuncs.com/ok.png"
    )

    downloaded = store.save_downloaded_artifact(
        kind="image",
        model_id=IMAGE_MODEL_ID,
        prompt="prompt",
        negative_prompt="",
        request_id="req",
        latency_ms=1,
        client=client,
        download_url="https://dashscope-result-1.aliyuncs.com/ok.png",
        usage={},
        cost=None,
        mime_prefix="image/",
    )
    assert downloaded.manifest["mime_type"] == "image/png"

    with pytest.raises(ValueError):
        store.save_downloaded_artifact(
            kind="image",
            model_id=IMAGE_MODEL_ID,
            prompt="prompt",
            negative_prompt="",
            request_id="req",
            latency_ms=1,
            client=client,
            download_url="https://dashscope-result-1.aliyuncs.com/redirect.png",
            usage={},
            cost=None,
            mime_prefix="image/",
        )
    assert calls.count("https://dashscope-result-1.aliyuncs.com/redirect.png") == 1


def test_media_download_timeout_retries_then_commits_one_real_artifact(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    signed_url = "https://dashscope-result-1.aliyuncs.com/image.png?signature=jury-secret"
    get_timeouts: list[dict[str, float]] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        get_timeouts.append(request.extensions["timeout"])
        if len(get_timeouts) == 1:
            raise httpx.ConnectTimeout(f"connection failed for {signed_url}", request=request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"verified-real-bytes")

    store = make_store(
        tmp_path,
        clock,
        download_timeout_seconds=147.0,
        download_retry_delay_seconds=0.25,
        sleeper=sleeps.append,
    )
    downloaded = store.save_downloaded_artifact(
        kind="image",
        model_id=IMAGE_MODEL_ID,
        prompt="jury image",
        negative_prompt="watermark",
        request_id="req-download-retry",
        latency_ms=10,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        download_url=signed_url,
        usage={"width": 1792, "height": 1008},
        cost=None,
        mime_prefix="image/",
        width=1792,
        height=1008,
    )

    assert len(get_timeouts) == DEFAULT_DOWNLOAD_MAX_ATTEMPTS
    assert all(timeout["read"] == 147.0 for timeout in get_timeouts)
    assert sleeps == [0.25]
    assert downloaded.manifest["byte_size"] == len(b"verified-real-bytes")
    assert downloaded.manifest["sha256"]
    assert downloaded.manifest["width"] == 1792
    assert downloaded.manifest["height"] == 1008
    assert len(list((store.root_dir / "manifests").glob("*.json"))) == 1
    assert not list(store.root_dir.rglob("*.part"))


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_media_download_transient_status_retries_once(status_code, tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    get_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            return httpx.Response(status_code, content=b"transient")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"image")

    store = make_store(tmp_path, clock, sleeper=sleeps.append)
    downloaded = store.save_downloaded_artifact(
        kind="image",
        model_id=IMAGE_MODEL_ID,
        prompt="prompt",
        negative_prompt="",
        request_id="req-transient",
        latency_ms=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        download_url=IMAGE_URL_1,
        usage={},
        cost=None,
        mime_prefix="image/",
    )

    assert downloaded.manifest["byte_size"] == len(b"image")
    assert get_count == 2
    assert sleeps == [1.0]
    assert len(list((store.root_dir / "manifests").glob("*.json"))) == 1


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_media_download_permanent_status_is_not_retried_or_leaked(status_code, tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    signed_url = "https://dashscope-result-1.aliyuncs.com/image.png?signature=never-leak"
    get_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        get_count += 1
        return httpx.Response(status_code, content=b"permanent")

    store = make_store(tmp_path, clock, sleeper=sleeps.append)
    with pytest.raises(ValueError) as raised:
        store.save_downloaded_artifact(
            kind="image",
            model_id=IMAGE_MODEL_ID,
            prompt="prompt",
            negative_prompt="",
            request_id="req-permanent",
            latency_ms=1,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            download_url=signed_url,
            usage={},
            cost=None,
            mime_prefix="image/",
        )

    assert str(status_code) in str(raised.value)
    assert "never-leak" not in str(raised.value)
    assert get_count == 1
    assert sleeps == []
    assert not list(store.root_dir.rglob("*.part"))
    assert not list((store.root_dir / "manifests").glob("*.json"))


def test_media_download_timeout_exhaustion_cleans_partial_bytes_and_redacts_url(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    signed_url = "https://dashscope-result-1.aliyuncs.com/image.png?signature=never-leak"
    attempts = 0
    sleeps: list[float] = []

    class PartialThenTimeout(httpx.SyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self.request = request

        def __iter__(self):
            yield b"partial-bytes"
            raise httpx.ReadTimeout(f"timed out reading {signed_url}", request=self.request)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=PartialThenTimeout(request))

    store = make_store(tmp_path, clock, sleeper=sleeps.append)
    with pytest.raises(TimeoutError) as raised:
        store.save_downloaded_artifact(
            kind="image",
            model_id=IMAGE_MODEL_ID,
            prompt="prompt",
            negative_prompt="",
            request_id="req-timeout-exhausted",
            latency_ms=1,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            download_url=signed_url,
            usage={},
            cost=None,
            mime_prefix="image/",
        )

    assert attempts == DEFAULT_DOWNLOAD_MAX_ATTEMPTS
    assert sleeps == [1.0]
    assert "never-leak" not in str(raised.value)
    assert not list(store.root_dir.rglob("*.part"))
    assert not list((store.root_dir / "artifacts").iterdir())
    assert not list((store.root_dir / "manifests").iterdir())
    assert DEFAULT_DOWNLOAD_TIMEOUT_SECONDS > 120.0


def test_image_request_shape_immediate_download_manifest_inspect_publish_and_restart(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    requests: list[tuple[str, dict[str, str], Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests.append((str(request.url), dict(request.headers), json.loads(request.content.decode("utf-8"))))
            return httpx.Response(
                200,
                json={
                    "request_id": "req-image-1",
                    "output": {"choices": [{"message": {"content": [{"image": IMAGE_URL_1}, {"image": IMAGE_URL_2}]}}]},
                    "usage": {"width": 1024, "height": 768, "image_count": 2},
                },
            )
        if str(request.url) == IMAGE_URL_1:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"image-one")
        if str(request.url) == IMAGE_URL_2:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"image-two")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = make_store(tmp_path, clock)
    tools = ImageGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.monotonic)

    result = tools.generate_images("castle at dawn", "low quality", 1024, 768, 2, 11)

    assert result["success"] is True
    assert requests[0][0].endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert requests[0][1].get("Authorization", requests[0][1].get("authorization")) == "Bearer dash-key-secret"
    assert requests[0][2] == {
        "model": IMAGE_MODEL_ID,
        "input": {"messages": [{"role": "user", "content": [{"text": "castle at dawn"}]}]},
        "parameters": {
            "negative_prompt": "low quality",
            "prompt_extend": True,
            "watermark": False,
            "size": "1024*768",
            "n": 2,
            "seed": 11,
        },
    }
    artifact_id = result["data"]["artifact_ids"][0]
    manifest = tools.inspect_image(artifact_id)["data"]["artifact"]
    assert manifest["kind"] == "image"
    assert manifest["mime_type"] == "image/png"
    assert manifest["byte_size"] == len(b"image-one")
    assert manifest["prompt_hash"]
    assert manifest["negative_prompt_hash"]
    assert manifest["published"] is False
    assert "aliyuncs.com" not in json.dumps(result)
    published = tools.publish_image(artifact_id, "release hero image")
    assert published["success"] is True
    assert published["data"]["artifact"]["published"] is True

    restarted = ImageGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.monotonic)
    assert restarted.inspect_image(artifact_id)["data"]["artifact"]["artifact_id"] == artifact_id


def test_image_timeout_retries_once_with_configured_deadline_then_preserves_real_artifact(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    post_timeouts: list[dict[str, float]] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            post_timeouts.append(request.extensions["timeout"])
            if len(post_timeouts) == 1:
                raise httpx.ReadTimeout("provider is still rendering", request=request)
            return httpx.Response(
                200,
                json={
                    "request_id": "req-image-after-timeout",
                    "output": {"choices": [{"message": {"content": [{"image": IMAGE_URL_1}]}}]},
                    "usage": {"width": 1792, "height": 1008, "image_count": 1},
                },
            )
        if str(request.url) == IMAGE_URL_1:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"real-image")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=make_store(tmp_path, clock),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
        sleeper=sleeps.append,
        request_timeout_seconds=123.0,
    )

    result = tools.generate_images("jury launch visual", width=1792, height=1008)

    assert result["success"] is True
    assert len(result["data"]["artifact_ids"]) == 1
    assert len(post_timeouts) == DEFAULT_IMAGE_REQUEST_MAX_ATTEMPTS
    assert all(timeout["read"] == 123.0 for timeout in post_timeouts)
    assert sleeps == [2.0]
    manifest = result["data"]["artifacts"][0]
    assert manifest["model_id"] == IMAGE_MODEL_ID
    assert manifest["width"] == 1792
    assert manifest["height"] == 1008
    assert manifest["sha256"]


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_image_transient_http_status_retries_once(status_code, tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    post_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            if post_count == 1:
                return httpx.Response(status_code, json={"message": "transient"})
            return httpx.Response(
                200,
                json={
                    "request_id": "req-image-http-retry",
                    "output": {"choices": [{"message": {"content": [{"image": IMAGE_URL_1}]}}]},
                    "usage": {"width": 512, "height": 512},
                },
            )
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"real-image")

    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=make_store(tmp_path, clock),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
        sleeper=sleeps.append,
    )

    result = tools.generate_images("prompt", width=512, height=512)

    assert result["success"] is True
    assert post_count == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_image_permanent_http_status_does_not_retry(status_code, tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    post_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(status_code, json={"message": "permanent"})

    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=make_store(tmp_path, clock),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
        sleeper=sleeps.append,
    )

    result = tools.generate_images("prompt", width=512, height=512)

    assert result["success"] is False
    assert result["error_code"] == "provider_image_http"
    assert post_count == 1
    assert sleeps == []
    assert "dash-key-secret" not in json.dumps(result)


def test_image_timeout_exhaustion_is_bounded_and_secret_safe(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    post_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        raise httpx.ReadTimeout("secret provider detail", request=request)

    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=make_store(tmp_path, clock),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
        sleeper=sleeps.append,
    )

    result = tools.generate_images("prompt", width=512, height=512)

    assert result == {
        "success": False,
        "data": {"artifact_ids": []},
        "error_code": "network_image_timeout",
        "error_message": "Image request timed out",
    }
    assert post_count == DEFAULT_IMAGE_REQUEST_MAX_ATTEMPTS
    assert sleeps == [2.0]
    assert DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS > 60.0


def test_image_partial_failure_redaction_mime_cap_cleanup_and_fsync(tmp_path):
    clock = Clock(datetime(2026, 7, 14, tzinfo=UTC))
    fsync_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "req-image-2",
                    "output": {"choices": [{"message": {"content": [{"image": IMAGE_URL_1}, {"image": IMAGE_URL_2}]}}]},
                    "usage": {"width": 512, "height": 512, "image_count": 2},
                },
            )
        if str(request.url) == IMAGE_URL_1:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok-image")
        if str(request.url) == IMAGE_URL_2:
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"bad")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = make_store(tmp_path, clock, max_download_bytes=4, directory_fsync=lambda path: fsync_calls.append(path.name))
    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
    )

    oversized = tools.generate_images("prompt", width=512, height=512, count=1)
    assert oversized["success"] is False
    assert oversized["error_code"] == "download_image_partial"

    store = make_store(tmp_path, clock, max_download_bytes=1024, directory_fsync=lambda path: fsync_calls.append(path.name))
    tools = ImageGenerationTools(
        settings=make_settings(),
        artifact_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.monotonic,
    )
    partial = tools.generate_images("prompt secret text", "negative secret", 512, 512, 2, 1)
    assert partial["success"] is False
    assert len(partial["data"]["artifact_ids"]) == 1
    assert "aliyuncs.com" not in json.dumps(partial)
    assert "prompt secret text" not in json.dumps(partial)
    assert not list(store.root_dir.rglob("*.tmp"))
    assert fsync_calls


def test_video_submit_request_shape_and_private_persistence(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    requests: list[tuple[str, dict[str, str], Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), dict(request.headers), json.loads(request.content.decode("utf-8"))))
        return httpx.Response(200, json={"request_id": "req-video-1", "output": {"task_id": "provider-task-1", "task_status": "PENDING"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = make_store(tmp_path, clock)
    tools = VideoGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.timestamp)

    result = tools.submit_text_to_video("launch trailer", "blur", 10, "1280*720", 7)

    assert result["success"] is True
    assert requests[0][0].endswith("/api/v1/services/aigc/video-generation/video-synthesis")
    assert requests[0][1].get("Authorization", requests[0][1].get("authorization")) == "Bearer dash-key-secret"
    assert requests[0][1].get("X-DashScope-Async", requests[0][1].get("x-dashscope-async")) == "enable"
    assert requests[0][2]["parameters"]["resolution"] == "720P"
    job = result["data"]["job"]
    assert job["job_id"].startswith("job_")
    assert "provider-task-1" not in json.dumps(result)
    private_record = store.load_video_job(job["job_id"])
    assert private_record["provider_task_id"] == "provider-task-1"
    assert private_record["version"] == 1


def test_video_one_poll_backoff_cancel_honesty_and_restart_recovery(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    calls: list[str] = []
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req-video-2", "output": {"task_id": "provider-task-2", "task_status": "PENDING"}})
        if request.method == "GET" and str(request.url).endswith("/tasks/provider-task-2"):
            state["polls"] += 1
            if state["polls"] == 1:
                return httpx.Response(200, json={"request_id": "req-poll-1", "output": {"task_status": "RUNNING"}, "usage": {"video_duration": 10}})
            return httpx.Response(200, json={"request_id": "req-poll-2", "output": {"task_status": "SUCCEEDED", "video_url": VIDEO_URL}, "usage": {"video_duration": 10}})
        if str(request.url) == VIDEO_URL:
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video-data")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = make_store(tmp_path, clock)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    tools = VideoGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.timestamp)

    job_id = tools.submit_text_to_video("trailer", "", 10, "1280*720", 3)["data"]["job"]["job_id"]
    first_poll = tools.get_video_job(job_id)
    assert first_poll["data"]["job"]["status"] == "RUNNING"
    assert len([item for item in calls if item.startswith("GET ")]) == 1
    assert tools.get_video_job(job_id)["data"]["job"]["status"] == "RUNNING"

    clock.advance(2)
    restarted = VideoGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.timestamp)
    second_poll = restarted.get_video_job(job_id)
    assert second_poll["data"]["job"]["status"] == "SUCCEEDED"

    canceled = restarted.cancel_video_job(job_id)
    assert canceled["data"]["job"]["cancellation_requested"] is True
    assert canceled["data"]["job"]["provider_cancel_supported"] is False
    assert canceled["data"]["job"]["provider_canceled"] is False

    collected = restarted.collect_video(job_id)
    artifact_id = collected["data"]["artifact"]["artifact_id"]
    assert restarted.collect_video(job_id)["data"]["artifact"]["artifact_id"] == artifact_id
    assert restarted.inspect_video(artifact_id)["data"]["artifact"]["kind"] == "video"


def test_video_failure_unknown_and_expiry_paths(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    poll_status = {"value": "FAILED"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req-video-3", "output": {"task_id": "provider-task-3", "task_status": "PENDING"}})
        return httpx.Response(200, json={"request_id": "req-poll-3", "output": {"task_status": poll_status["value"]}})

    store = make_store(tmp_path, clock)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    tools = VideoGenerationTools(settings=make_settings(), artifact_store=store, http_client=client, clock=clock.timestamp)
    job_id = tools.submit_text_to_video("trailer", "", 5, "960*960", None)["data"]["job"]["job_id"]

    failed = tools.get_video_job(job_id)
    assert failed["data"]["job"]["status"] == "FAILED"
    assert failed["data"]["job"]["error_category"] == "provider"

    poll_status["value"] = "UNKNOWN"
    second_job = tools.submit_text_to_video("trailer", "", 5, "960*960", None)["data"]["job"]["job_id"]
    unknown = tools.get_video_job(second_job)
    assert unknown["data"]["job"]["status"] == "UNKNOWN"

    expiring_job = store.create_video_job(
        job_id=None,
        model_id=VIDEO_MODEL_ID,
        prompt="prompt",
        negative_prompt="",
        request_id="req-x",
        provider_task_id="provider-task-expired",
        duration=5,
        size="1280*720",
        resolution="720P",
        ratio="16:9",
        seed=None,
        initial_status="PENDING",
    )
    record = store.load_video_job(expiring_job["job_id"])
    record["task_expires_at"] = "2026-07-14T11:00:00Z"
    with store.job_transaction(expiring_job["job_id"]):
        store.save_video_job(expiring_job["job_id"], record, expected_version=record["version"])
    expired = tools.get_video_job(expiring_job["job_id"])
    assert expired["data"]["job"]["status"] == "UNKNOWN"

    succeeded_job = store.create_video_job(
        job_id=None,
        model_id=VIDEO_MODEL_ID,
        prompt="prompt",
        negative_prompt="",
        request_id="req-y",
        provider_task_id="provider-task-url-expired",
        duration=5,
        size="1280*720",
        resolution="720P",
        ratio="16:9",
        seed=None,
        initial_status="SUCCEEDED",
    )
    record = store.load_video_job(succeeded_job["job_id"])
    record["provider_result_url"] = VIDEO_URL
    record["provider_result_url_expires_at"] = "2026-07-14T11:00:00Z"
    with store.job_transaction(succeeded_job["job_id"]):
        store.save_video_job(succeeded_job["job_id"], record, expected_version=record["version"])
    expired_url = tools.collect_video(succeeded_job["job_id"])
    assert expired_url["error_code"] == "provider_video_url_expired"


def test_stale_write_and_deterministic_poll_vs_cancel_no_lost_update(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    release_poll = threading.Event()
    poll_started = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req", "output": {"task_id": "provider-task-a", "task_status": "PENDING"}})
        poll_started.set()
        release_poll.wait(timeout=2)
        return httpx.Response(200, json={"request_id": "req-poll", "output": {"task_status": "RUNNING"}})

    store = make_store(tmp_path, clock)
    tools = VideoGenerationTools(
        settings=make_settings(),
        artifact_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.timestamp,
    )
    job_id = tools.submit_text_to_video("prompt")["data"]["job"]["job_id"]

    first = store.load_video_job(job_id)
    second = store.load_video_job(job_id)
    with store.job_transaction(job_id):
        saved = store.save_video_job(job_id, first, expected_version=first["version"])
    with store.job_transaction(job_id):
        with pytest.raises(StaleJobVersionError):
            store.save_video_job(job_id, second, expected_version=second["version"])
    assert saved["version"] == 2

    poll_result: dict[str, Any] = {}
    poll_thread = threading.Thread(target=lambda: poll_result.update(tools.get_video_job(job_id)))
    poll_thread.start()
    assert poll_started.wait(timeout=2)
    cancel_result = tools.cancel_video_job(job_id)
    release_poll.set()
    poll_thread.join(timeout=2)

    assert cancel_result["data"]["job"]["cancellation_requested"] is True
    assert poll_result["data"]["job"]["cancellation_requested"] is True
    final_record = store.load_video_job(job_id)
    assert final_record["cancellation_requested"] is True
    assert final_record["status"] == "PENDING"


def test_collect_video_is_serialized_and_returns_one_artifact(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    video_gets = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req", "output": {"task_id": "provider-task-a", "task_status": "SUCCEEDED"}})
        if str(request.url) == VIDEO_URL:
            video_gets["count"] += 1
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video-data")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = make_store(tmp_path, clock)
    tools = VideoGenerationTools(
        settings=make_settings(),
        artifact_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.timestamp,
    )
    job_id = tools.submit_text_to_video("prompt")["data"]["job"]["job_id"]
    record = store.load_video_job(job_id)
    record["status"] = "SUCCEEDED"
    record["provider_result_url"] = VIDEO_URL
    record["provider_result_url_expires_at"] = "2026-07-15T12:00:00Z"
    with store.job_transaction(job_id):
        store.save_video_job(job_id, record, expected_version=record["version"])

    results: list[dict[str, Any]] = [{}, {}]
    threads = [
        threading.Thread(target=lambda index=i: results[index].update(tools.collect_video(job_id)))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    artifact_ids = {result["data"]["artifact"]["artifact_id"] for result in results}
    assert len(artifact_ids) == 1
    assert video_gets["count"] == 1


def test_list_and_resume_pending_jobs_are_bounded(tmp_path):
    clock = Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    state = {"task1": "RUNNING", "task2": "SUCCEEDED"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            task_id = "provider-task-a" if "first" in request.content.decode("utf-8") else "provider-task-b"
            return httpx.Response(200, json={"request_id": "req", "output": {"task_id": task_id, "task_status": "PENDING"}})
        if str(request.url).endswith("/tasks/provider-task-a"):
            return httpx.Response(200, json={"request_id": "req-a", "output": {"task_status": state["task1"]}})
        if str(request.url).endswith("/tasks/provider-task-b"):
            return httpx.Response(200, json={"request_id": "req-b", "output": {"task_status": state["task2"], "video_url": VIDEO_URL}})
        if str(request.url) == VIDEO_URL:
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video-b")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = make_store(tmp_path, clock)
    tools = VideoGenerationTools(
        settings=make_settings(),
        artifact_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock.timestamp,
    )
    first = tools.submit_text_to_video("first prompt")["data"]["job"]["job_id"]
    second = tools.submit_text_to_video("second prompt")["data"]["job"]["job_id"]

    pending = tools.list_pending_jobs()
    assert {item["job_id"] for item in pending} == {first, second}
    assert len(tools.resume_pending_jobs(1)) == 1
    clock.advance(2)
    resumed = tools.resume_pending_jobs(2)
    assert 1 <= len(resumed) <= 2
