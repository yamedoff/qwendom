from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from society.provider_runtime import ProviderCallError, redact_provider_error, retry_after_seconds, run_provider_call


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_honors_retry_after_for_transient_failure(self) -> None:
        calls = 0
        retries: list[dict] = []

        async def call() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                error = RuntimeError("429 rate limit")
                error.response = SimpleNamespace(headers={"Retry-After": "0"})
                raise error
            return "ok"

        result = await run_provider_call(call, timeout_seconds=1, on_retry=retries.append)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        self.assertTrue(retries[0]["retry_after_honored"])

    async def test_does_not_retry_deterministic_failure_and_redacts_secret(self) -> None:
        async def call() -> str:
            raise ValueError("invalid request api_key=sk-supersecret123")

        with self.assertRaises(ProviderCallError) as raised:
            await run_provider_call(call, timeout_seconds=1)

        self.assertNotIn("supersecret", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    async def test_propagates_cancellation(self) -> None:
        async def call() -> str:
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await run_provider_call(call, timeout_seconds=1)

    def test_parses_retry_after_header(self) -> None:
        exc = RuntimeError("rate limit")
        exc.headers = {"retry-after": "3"}
        self.assertEqual(retry_after_seconds(exc), 3.0)

    def test_redacts_bearer_token(self) -> None:
        self.assertEqual(redact_provider_error("Authorization: Bearer abc123"), "Authorization: Bearer [REDACTED]")


if __name__ == "__main__":
    unittest.main()
