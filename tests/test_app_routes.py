import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

import app as app_module


class AppRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_module.app)

    def test_homepage_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("BillGuard AI", response.text)

    @patch("app.extract", new_callable=AsyncMock)
    def test_extract_endpoint_returns_validated_rows(self, mocked_extract):
        mocked_extract.return_value = {
            "vendor": "Example Supplies", "invoice_no": "INV-100",
            "date": "14/10/2026", "gstin": "27AAPFU0939F1ZV",
            "subtotal": 1000, "tax": 180, "total": 1180,
            "category": "Hardware",
        }
        image = Image.new("RGB", (40, 30), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        response = self.client.post(
            "/extract",
            files=[("files", ("invoice.png", buffer.getvalue(), "image/png"))],
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["vendor"], "Example Supplies")
        self.assertEqual(payload["rows"][0]["flags"], [])
        mocked_extract.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
