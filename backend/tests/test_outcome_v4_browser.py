from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.tests.test_agentbay_tools import make_tools, start_handle, agentbay_module


class _FakeAccessibility:
    def snapshot(self):
        return {"role": "WebArea", "name": "Fixture"}


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.method = "GET"
        self.resource_type = "document"
        self.url = url

    def response(self):
        return _FakeResponse(200)


class _FakeConsoleMessage:
    def __init__(self, text: str) -> None:
        self.type = "log"
        self.text = text


class _FakePage:
    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.accessibility = _FakeAccessibility()
        self.fail_with = fail_with
        self.viewport_calls: list[dict[str, int]] = []
        self.goto_calls: list[str] = []
        self.handlers: dict[str, object] = {}
        self.current_viewport = {"width": 0, "height": 0}
        self.closed = False

    def on(self, event_name: str, handler) -> None:
        self.handlers[event_name] = handler

    def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.current_viewport = dict(viewport)
        self.viewport_calls.append(dict(viewport))

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        if self.fail_with is not None:
            raise self.fail_with
        self.goto_calls.append(url)
        if "console" in self.handlers:
            self.handlers["console"](_FakeConsoleMessage(f"loaded {url}"))  # type: ignore[index]
        if "requestfinished" in self.handlers:
            self.handlers["requestfinished"](_FakeRequest(url))  # type: ignore[index]

    def wait_for_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def evaluate(self, script: str):
        if "button.click" in script:
            return {"interacted": True, "selector": "button", "text": "Review the launch checklist"}
        return {
            "url": self.goto_calls[-1] if self.goto_calls else "",
            "title": "Layer B Fixture",
            "viewport": dict(self.current_viewport),
            "hero_width": 960 if self.current_viewport["width"] == 1440 else 320,
            "hero_height": 480,
            "actionable_count": 1,
            "actionable_labels": ["Review the launch checklist"],
        }

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages: list[_FakePage] = []
        self._page = page

    def new_page(self) -> _FakePage:
        self.pages.append(self._page)
        return self._page


class _FakeCdpBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.contexts = [_FakeContext(page)]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, cdp_browser: _FakeCdpBrowser) -> None:
        self._cdp_browser = cdp_browser
        self.endpoints: list[str] = []

    def connect_over_cdp(self, endpoint: str) -> _FakeCdpBrowser:
        self.endpoints.append(endpoint)
        return self._cdp_browser


class _FakePlaywrightManager:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def start(self) -> "_FakePlaywrightManager":
        return self

    def stop(self) -> None:
        self.stopped = True


class _FakeBrowserOption:
    def __init__(self, viewport):
        self.viewport = viewport


class _FakeBrowserViewport:
    """Mirror the AgentBay viewport value used to initialize the browser."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


class _FakeBrowserWrapper:
    def __init__(self, session) -> None:
        self.session = session
        self.initialized = []
        self.screenshot_calls: list[dict[str, object]] = []
        self.closed = False
        self.endpoint = "ws://secret-cdp-endpoint"

    def initialize(self, option: _FakeBrowserOption) -> bool:
        self.initialized.append(option.viewport)
        return True

    def get_endpoint_url(self) -> str:
        return self.endpoint

    def screenshot(self, page: _FakePage, full_page: bool, type: str) -> bytes:
        self.screenshot_calls.append({"viewport": dict(page.current_viewport), "full_page": full_page, "type": type})
        label = "desktop" if page.current_viewport["width"] == 1440 else "mobile"
        return f"png-{label}".encode("utf-8")

    def close(self) -> None:
        self.closed = True


def _install_browser_mocks(monkeypatch: pytest.MonkeyPatch, *, fail_with: BaseException | None = None):
    page = _FakePage(fail_with=fail_with)
    cdp_browser = _FakeCdpBrowser(page)
    chromium = _FakeChromium(cdp_browser)
    playwright = _FakePlaywrightManager(chromium)
    browser_wrapper = _FakeBrowserWrapper(session=object())
    server_calls = {"start": 0, "stop": 0}

    monkeypatch.setattr(
        agentbay_module,
        "_load_agentbay_browser_types",
        lambda: (_FakeBrowserWrapper, _FakeBrowserOption, _FakeBrowserViewport),
    )
    monkeypatch.setattr(agentbay_module, "_load_sync_playwright", lambda: (lambda: playwright))

    def fake_start_server(self, state, entrypoint):
        del state, entrypoint
        server_calls["start"] += 1
        return "req-browser-start", "4321"

    def fake_stop_server(self, state, server_pid):
        del state, server_pid
        server_calls["stop"] += 1
        return "req-browser-stop"

    monkeypatch.setattr(agentbay_module.AgentBayTools, "_start_browser_server", fake_start_server)
    monkeypatch.setattr(agentbay_module.AgentBayTools, "_stop_browser_server", fake_stop_server)
    return page, browser_wrapper, chromium, playwright, server_calls


def test_browser_render_captures_two_local_viewports_exports_artifacts_and_redacts(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    tools, _client, _ = make_tools(tmp_path, role_key="frontend_engineer", event_sink=events.append)
    handle = start_handle(tools)
    page, _browser_wrapper, chromium, playwright, server_calls = _install_browser_mocks(monkeypatch)

    result = tools.browser_render(handle)

    assert result["success"] is True
    assert page.viewport_calls == [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]
    assert page.goto_calls == [
        "http://127.0.0.1:38451/index.html",
        "http://127.0.0.1:38451/index.html",
    ]
    assert chromium.endpoints == ["ws://secret-cdp-endpoint"]
    assert result["data"]["browser_render_credits"] == 2
    assert [item["viewport_name"] for item in result["data"]["viewports"]] == ["desktop", "mobile"]
    artifact_paths = [Path(item["path"]) for item in result["artifact_references"]]
    assert len(artifact_paths) == 5
    assert all(path.exists() for path in artifact_paths)
    assert server_calls == {"start": 1, "stop": 1}
    assert playwright.stopped is True
    assert "sess-secret-123" not in str(result)
    assert "ws://secret-cdp-endpoint" not in str(events)
    assert any(event["event_type"] == "agentbay_browser_render_cleanup_completed" for event in events)


def test_browser_render_rejects_arbitrary_entrypoint_without_bootstrap(monkeypatch, tmp_path):
    tools, _client, _ = make_tools(tmp_path, role_key="frontend_engineer")
    handle = start_handle(tools)
    calls = {"start": 0}

    def fake_start_server(self, state, entrypoint):
        del self, state, entrypoint
        calls["start"] += 1
        return "req-browser-start", "4321"

    monkeypatch.setattr(agentbay_module.AgentBayTools, "_start_browser_server", fake_start_server)

    result = tools.browser_render(handle, "/workspace/other/index.html")

    assert result["success"] is False
    assert result["error_code"] == "agentbay_browser_render_rejected"
    assert calls["start"] == 0


def test_browser_render_reuses_one_environment_and_enforces_per_handle_cap(monkeypatch, tmp_path):
    tools, _client, _ = make_tools(tmp_path, role_key="frontend_engineer")
    handle = start_handle(tools)
    page, _browser_wrapper, _chromium, _playwright, server_calls = _install_browser_mocks(monkeypatch)

    first = tools.browser_render(handle)
    second = tools.browser_render(handle)

    assert first["success"] is True
    assert second["success"] is False
    assert second["error_code"] == "agentbay_browser_render_exception"
    assert "limit reached" in second["error_message"]
    assert page.viewport_calls == [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]
    assert server_calls == {"start": 1, "stop": 1}


def test_browser_render_cleans_up_on_runtime_error(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    tools, _client, _ = make_tools(tmp_path, role_key="frontend_engineer", event_sink=events.append)
    handle = start_handle(tools)
    page, _browser_wrapper, _chromium, playwright, server_calls = _install_browser_mocks(monkeypatch, fail_with=RuntimeError("boom"))

    result = tools.browser_render(handle)

    assert result["success"] is False
    assert result["error_code"] == "agentbay_browser_render_exception"
    assert page.closed is True
    assert playwright.stopped is True
    assert server_calls == {"start": 1, "stop": 1}
    assert any(event["event_type"] == "agentbay_browser_render_cleanup_completed" for event in events)


def test_browser_render_cleans_up_on_cancel(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    tools, _client, _ = make_tools(tmp_path, role_key="frontend_engineer", event_sink=events.append)
    handle = start_handle(tools)
    page, _browser_wrapper, _chromium, playwright, server_calls = _install_browser_mocks(
        monkeypatch,
        fail_with=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        tools.browser_render(handle)

    assert page.closed is True
    assert playwright.stopped is True
    assert server_calls == {"start": 1, "stop": 1}
    assert any(event["event_type"] == "agentbay_browser_render_cleanup_completed" for event in events)
