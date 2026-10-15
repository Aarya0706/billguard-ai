import unittest
from validate import validate, to_iso_date, to_num


class ValidationTests(unittest.TestCase):
    def good_invoice(self):
        return {
            "vendor": "Example Supplies", "invoice_no": "INV-100",
            "date": "14/10/2026", "gstin": "27AAPFU0939F1ZV",
            "subtotal": "1,000.00", "tax": "180.00", "total": "1,180.00",
            "category": "hardware",
        }

    def test_valid_invoice_passes_basic_checks(self):
        result = validate(self.good_invoice())
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["date"], "2026-10-14")
        self.assertEqual(result["category"], "Hardware")
        self.assertEqual(result["total"], 1180.0)

    def test_invalid_gstin_is_flagged(self):
        raw = self.good_invoice()
        raw["gstin"] = "23AAFC5778BL1ZT"
        result = validate(raw)
        self.assertIn("GSTIN format invalid", result["flags"])

    def test_arithmetic_mismatch_is_flagged(self):
        raw = self.good_invoice()
        raw["total"] = 1299
        result = validate(raw)
        self.assertIn("subtotal + tax does not match total", result["flags"])

    def test_missing_required_values_are_flagged(self):
        result = validate({"vendor": "Example Supplies", "category": "Other"})
        for expected in ("date missing or unreadable", "total missing or unreadable",
                         "subtotal missing or unreadable", "tax missing or unreadable"):
            self.assertIn(expected, result["flags"])

    def test_currency_formats(self):
        self.assertEqual(to_num("₹ 1,23,456.70"), 123456.70)
        self.assertEqual(to_num("Rs. 499/-"), 499.0)
        self.assertIsNone(to_num("not a number"))

    def test_dates(self):
        self.assertEqual(to_iso_date("2026-10-14T10:15:00"), "2026-10-14")
        self.assertIsNone(to_iso_date("tomorrow"))


if __name__ == "__main__":
    unittest.main()
