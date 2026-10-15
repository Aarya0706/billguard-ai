import unittest
from io import BytesIO

import fitz
from PIL import Image

from app import images_for_file, InvoiceInputError, parse_model_response


class ImagePreparationTests(unittest.TestCase):
    def test_jpeg_is_normalized_to_png_data_url(self):
        image = Image.new("RGB", (100, 60), "white")
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        urls = images_for_file("sample.jpg", buffer.getvalue())
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("data:image/png;base64,"))

    def test_all_pdf_pages_are_returned(self):
        document = fitz.open()
        for text in ("Invoice page one", "Grand total on page two"):
            page = document.new_page()
            page.insert_text((72, 72), text)
        data = document.tobytes()
        document.close()
        urls = images_for_file("two-pages.pdf", data)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(url.startswith("data:image/png;base64,") for url in urls))

    def test_model_json_in_markdown_fence_is_parsed(self):
        parsed = parse_model_response("```json\n{\"vendor\": \"ACME\"}\n```")
        self.assertEqual(parsed["vendor"], "ACME")

    def test_unsupported_file_is_rejected(self):
        with self.assertRaises(InvoiceInputError):
            images_for_file("invoice.exe", b"not an invoice")


if __name__ == "__main__":
    unittest.main()
