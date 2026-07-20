from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.provider_preflight import model_capability_preflight


class ProviderPreflightTests(unittest.TestCase):
    def make_settings(self, **overrides: str) -> Settings:
        base = {
            "LLM_PROVIDER": "qwen",
            "QWEN_API_KEY": "llm-test-key",
            "QWEN_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "QWEN_MODEL": "qwen3.7-plus",
            "AGENTBAY_API_KEY": "agentbay-test-key",
            "AGENTBAY_ENDPOINT": "wuyingai.ap-southeast-1.aliyuncs.com",
            "AGENTBAY_REGION_ID": "ap-southeast-1",
            "AGENTBAY_IMAGE_ID": "code_latest",
            "MODEL_STUDIO_WORKSPACE_ID": "workspace-secret",
            "QWEN_IMAGE_MODEL": "qwen-image-2.0-pro-2026-06-22",
            "WAN_VIDEO_MODEL": "wan2.7-t2v-2026-06-12",
        }
        base.update(overrides)
        return Settings(**base)

    def blocker_codes(self, result: dict[str, object]) -> set[str]:
        blockers = result["typed_blockers"]
        self.assertIsInstance(blockers, list)
        return {blocker["code"] for blocker in blockers}  # type: ignore[index]

    def test_legacy_contract_fields_remain_available(self) -> None:
        result = model_capability_preflight(self.make_settings(), agentbay_sdk_available=True)

        self.assertTrue(result["ready"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["model"], "qwen3.7-plus")
        self.assertTrue(result["direct_qwen_cloud"])
        self.assertTrue(result["structured_tools_supported"])
        self.assertEqual(result["reason"], "ready")

    def test_fully_ready_static_configuration_reports_roadmap_ready(self) -> None:
        result = model_capability_preflight(
            self.make_settings(DASHSCOPE_API_KEY="media-test-key"),
            agentbay_sdk_available=True,
        )

        self.assertTrue(result["roadmap_ready"])
        self.assertEqual(result["typed_blockers"], [])
        self.assertTrue(result["provider_services"]["agentbay"]["ready"])
        self.assertTrue(result["provider_services"]["image"]["ready"])
        self.assertTrue(result["provider_services"]["video"]["ready"])

    def test_fully_ready_static_configuration_accepts_dashscope_intl_default(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                DASHSCOPE_API_KEY="media-test-key",
            ),
            agentbay_sdk_available=True,
        )

        self.assertTrue(result["direct_qwen_cloud"])
        self.assertTrue(result["roadmap_ready"])
        self.assertEqual(result["typed_blockers"], [])
        self.assertTrue(result["provider_services"]["image"]["ready"])
        self.assertTrue(result["provider_services"]["video"]["ready"])

    def test_missing_provider_key_preserves_legacy_reason(self) -> None:
        result = model_capability_preflight(
            self.make_settings(QWEN_API_KEY="", DASHSCOPE_API_KEY=""),
            agentbay_sdk_available=True,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "missing provider API key")
        self.assertIn("media_api_key_missing", self.blocker_codes(result))

    def test_unsupported_qwen_model_is_rejected_with_typed_blocker(self) -> None:
        result = model_capability_preflight(
            self.make_settings(QWEN_MODEL="qwen-unknown"),
            agentbay_sdk_available=True,
        )

        self.assertFalse(result["ready"])
        self.assertFalse(result["structured_tools_supported"])
        self.assertIn("qwen_model_not_supported", self.blocker_codes(result))

    def test_missing_agentbay_key_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(AGENTBAY_API_KEY=""),
            agentbay_sdk_available=True,
        )

        self.assertIn("agentbay_api_key_missing", self.blocker_codes(result))
        self.assertFalse(result["provider_services"]["agentbay"]["ready"])

    def test_missing_model_studio_workspace_uses_qwen_cloud_fallback(self) -> None:
        result = model_capability_preflight(
            self.make_settings(MODEL_STUDIO_WORKSPACE_ID=""),
            agentbay_sdk_available=True,
        )

        self.assertNotIn("model_studio_workspace_missing", self.blocker_codes(result))
        self.assertTrue(result["provider_services"]["image"]["configured"])
        self.assertTrue(result["provider_services"]["video"]["configured"])

    def test_media_configured_with_only_qwen_api_key_and_no_workspace(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                DASHSCOPE_API_KEY="",
                MODEL_STUDIO_WORKSPACE_ID="",
                MODEL_STUDIO_BASE_URL="",
            ),
            agentbay_sdk_available=True,
        )

        self.assertEqual(result["typed_blockers"], [])
        self.assertTrue(result["provider_services"]["image"]["ready"])
        self.assertTrue(result["provider_services"]["video"]["ready"])

    def test_missing_media_key_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(QWEN_API_KEY="", DASHSCOPE_API_KEY=""),
            agentbay_sdk_available=True,
        )

        self.assertIn("media_api_key_missing", self.blocker_codes(result))
        self.assertFalse(result["provider_services"]["image"]["configured"])

    def test_sdk_override_false_reports_missing_sdk(self) -> None:
        result = model_capability_preflight(self.make_settings(), agentbay_sdk_available=False)

        self.assertIn("agentbay_sdk_missing", self.blocker_codes(result))
        self.assertFalse(result["provider_services"]["agentbay"]["sdk_available"])

    def test_non_singapore_agentbay_endpoint_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(AGENTBAY_ENDPOINT="wuyingai.us-west-1.aliyuncs.com"),
            agentbay_sdk_available=True,
        )

        self.assertIn("agentbay_endpoint_not_singapore", self.blocker_codes(result))

    def test_non_singapore_agentbay_region_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(AGENTBAY_REGION_ID="us-west-1"),
            agentbay_sdk_available=True,
        )

        self.assertIn("agentbay_region_not_singapore", self.blocker_codes(result))

    def test_non_singapore_qwen_base_url_is_reported_and_blocks_roadmap(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                QWEN_BASE_URL="https://dashscope.us-west-1.aliyuncs.com/compatible-mode/v1",
                DASHSCOPE_API_KEY="media-test-key",
            ),
            agentbay_sdk_available=True,
        )

        self.assertFalse(result["direct_qwen_cloud"])
        self.assertIn("qwen_base_url_not_singapore", self.blocker_codes(result))
        self.assertFalse(result["roadmap_ready"])
        self.assertFalse(result["provider_services"]["image"]["ready"])
        self.assertFalse(result["provider_services"]["video"]["ready"])

    def test_non_singapore_model_studio_url_override_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                MODEL_STUDIO_WORKSPACE_ID="",
                MODEL_STUDIO_BASE_URL="https://workspace.us-west-1.maas.aliyuncs.com",
            ),
            agentbay_sdk_available=True,
        )

        self.assertIn("model_studio_url_not_singapore", self.blocker_codes(result))

    def test_invalid_qwen_base_url_reports_missing_media_base_without_workspace(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                MODEL_STUDIO_WORKSPACE_ID="",
                MODEL_STUDIO_BASE_URL="",
                QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/evil-path",
                DASHSCOPE_API_KEY="media-test-key",
            ),
            agentbay_sdk_available=True,
        )

        codes = self.blocker_codes(result)
        self.assertIn("qwen_base_url_not_singapore", codes)
        self.assertIn("media_base_url_missing", codes)
        self.assertFalse(result["provider_services"]["image"]["configured"])

    def test_explicit_media_base_override_takes_precedence_over_workspace_and_qwen_base(self) -> None:
        settings = self.make_settings(
            MODEL_STUDIO_BASE_URL="https://dashscope-intl.aliyuncs.com/api/v1",
            MODEL_STUDIO_WORKSPACE_ID="workspace-secret",
            QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        result = model_capability_preflight(settings, agentbay_sdk_available=True)

        self.assertEqual(settings.resolved_model_studio_base_url, "https://dashscope-intl.aliyuncs.com/api/v1")
        self.assertTrue(result["provider_services"]["image"]["configured"])
        self.assertEqual(result["typed_blockers"], [])

    def test_workspace_media_base_takes_precedence_over_qwen_fallback(self) -> None:
        settings = self.make_settings(
            MODEL_STUDIO_BASE_URL="",
            MODEL_STUDIO_WORKSPACE_ID="workspace-secret",
            QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        result = model_capability_preflight(settings, agentbay_sdk_available=True)

        self.assertEqual(
            settings.resolved_model_studio_base_url,
            "https://workspace-secret.ap-southeast-1.maas.aliyuncs.com/api/v1",
        )
        self.assertEqual(result["typed_blockers"], [])

    def test_unapproved_agentbay_image_id_is_reported(self) -> None:
        result = model_capability_preflight(
            self.make_settings(AGENTBAY_IMAGE_ID="custom-image"),
            agentbay_sdk_available=True,
        )

        self.assertIn("agentbay_image_not_supported", self.blocker_codes(result))

    def test_unfrozen_media_models_are_reported_separately(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                QWEN_IMAGE_MODEL="qwen-image-latest",
                WAN_VIDEO_MODEL="wan-latest",
            ),
            agentbay_sdk_available=True,
        )

        codes = self.blocker_codes(result)
        self.assertIn("qwen_image_model_unfrozen", codes)
        self.assertIn("wan_video_model_unfrozen", codes)

    def test_media_key_falls_back_to_qwen_key(self) -> None:
        settings = self.make_settings(DASHSCOPE_API_KEY="")
        result = model_capability_preflight(settings, agentbay_sdk_available=True)

        self.assertEqual(settings.media_api_key, "llm-test-key")
        self.assertNotIn("media_api_key_missing", self.blocker_codes(result))
        self.assertTrue(result["provider_services"]["image"]["configured"])

    def test_response_never_leaks_secrets_or_workspace_id(self) -> None:
        result = model_capability_preflight(
            self.make_settings(
                QWEN_API_KEY="llm-secret",
                DASHSCOPE_API_KEY="media-secret",
                AGENTBAY_API_KEY="agentbay-secret",
                MODEL_STUDIO_WORKSPACE_ID="workspace-secret",
            ),
            agentbay_sdk_available=False,
        )

        rendered = str(result)
        self.assertNotIn("llm-secret", rendered)
        self.assertNotIn("media-secret", rendered)
        self.assertNotIn("agentbay-secret", rendered)
        self.assertNotIn("workspace-secret", rendered)


if __name__ == "__main__":
    unittest.main()
