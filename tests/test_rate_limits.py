import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import app

DAILY = {"error": {"message": "Rate limit reached on tokens per day (TPD)"}}


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app._exhausted.clear()

    async def test_daily_quota_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(429, json=DAILY)

        with patch.object(app, "FALLBACK_MODELS", []):
            async with mock_client(handler) as client:
                with self.assertRaises(app.QuotaExhausted):
                    await app._post_with_retry(client, {})
        self.assertEqual(len(calls), 1)

    async def test_exhausted_models_skip_later_requests(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(429, json=DAILY)

        with patch.object(app, "FALLBACK_MODELS", []):
            async with mock_client(handler) as client:
                for _ in range(3):
                    with self.assertRaises(app.QuotaExhausted):
                        await app._post_with_retry(client, {})
        self.assertEqual(len(calls), 1)  # only the first call reached the provider

    async def test_walks_the_fallback_list(self):
        seen = []

        def handler(request):
            model = json.loads(request.content)["model"]
            seen.append(model)
            if model == "c":
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(429, json=DAILY)

        with patch.object(app, "MODEL", "a"), patch.object(app, "FALLBACK_MODELS", ["b", "c"]):
            async with mock_client(handler) as client:
                await app._post_with_retry(client, {})
        self.assertEqual(seen, ["a", "b", "c"])

    async def test_long_retry_after_fails_fast_without_sleeping(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"},
                                  json={"error": {"message": "slow down"}})

        with patch.object(app, "MAX_TOTAL_WAIT", 60), patch("asyncio.sleep", new=AsyncMock()) as sleep:
            async with mock_client(handler) as client:
                with self.assertRaises(app.RateLimitWait):
                    await app._post_with_retry(client, {})
        sleep.assert_not_called()

    async def test_short_rate_limit_then_success(self):
        responses = [httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow"}}),
                     httpx.Response(200, json={"choices": []})]
        async with mock_client(lambda request: responses.pop(0)) as client:
            with patch("asyncio.sleep", new=AsyncMock()) as sleep:
                await app._post_with_retry(client, {})
        sleep.assert_awaited_once()

    async def test_retries_exhausted_on_503(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={"error": {"message": "down"}})

        with patch.object(app, "MAX_RETRIES", 2), patch("asyncio.sleep", new=AsyncMock()):
            async with mock_client(handler) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    await app._post_with_retry(client, {})
        self.assertEqual(len(calls), 3)

    async def test_401_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        async with mock_client(handler) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await app._post_with_retry(client, {})
        self.assertEqual(len(calls), 1)

    async def test_timeout_propagates(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")

        async with mock_client(handler) as client:
            with self.assertRaises(httpx.TimeoutException):
                await app._post_with_retry(client, {})


class ParseAndRouteTests(unittest.TestCase):
    def test_malformed_model_output_is_rejected(self):
        for bad in ("not json", "[]", "", None):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                app.parse_model_response(bad)

    def test_quota_error_becomes_review_message(self):
        from io import BytesIO
        from fastapi.testclient import TestClient
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (20, 20), "white").save(buf, format="PNG")
        with patch("app.extract", new=AsyncMock(side_effect=app.QuotaExhausted("Daily quota used up"))):
            response = TestClient(app.app).post(
                "/extract", files=[("files", ("a.png", buf.getvalue(), "image/png"))])
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily quota used up", response.json()["rows"][0]["flags"][0])


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def run_health(self, handler, **kwargs):
        real = httpx.AsyncClient
        with patch("app.httpx.AsyncClient",
                   lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            return await app.health(**kwargs)

    async def test_prefixed_model_name_matches(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"id": f"models/{app.MODEL}"}]})
        result = await self.run_health(handler)
        self.assertEqual(result["status"], "ok")

    async def test_model_not_listed(self):
        result = await self.run_health(lambda r: httpx.Response(200, json={"data": [{"id": "other"}]}))
        self.assertEqual(result["status"], "model_not_listed")

    async def test_auth_failed(self):
        result = await self.run_health(lambda r: httpx.Response(401, json={}))
        self.assertEqual(result["status"], "auth_failed")

    async def test_endpoint_unavailable_message_is_provider_neutral(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        result = await self.run_health(handler)
        self.assertEqual(result["status"], "endpoint_unavailable")
        self.assertNotIn("Ollama", result["message"])

    async def test_probe_failure_reported(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": app.MODEL}]})
            return httpx.Response(500, json={"error": {"message": "boom"}})
        result = await self.run_health(handler, probe=True)
        self.assertEqual(result["status"], "model_request_failed")


if __name__ == "__main__":
    unittest.main()
