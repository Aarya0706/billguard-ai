"""Deterministic checks for invoice fields extracted by the vision model."""
from datetime import date, datetime
import os
import re

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y",
    "%d %B %Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
)
# Allowed difference (in rupees) between subtotal + tax and total; override with AMOUNT_TOLERANCE.
AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "1.0"))
CATEGORIES = (
    "Software", "Travel", "Meals", "Office", "Utilities", "Hardware",
    "Professional services", "Other",
)


def _text(value) -> str:
    """Convert a model field to a safe, trimmed string."""
    if value is None:
        return ""
    return str(value).strip()


def to_num(value):
    """Parse common invoice currency formats; return None when unreadable."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    cleaned = str(value).strip()
    for token in ("₹", "INR", "Rs.", "Rs", "rs.", "rs"):
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.replace(",", "").replace("/-", "").strip()
    # Allow accounting-style negatives, e.g. (125.00).
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1].strip()
    try:
        return round(float(cleaned), 2)
    except (ValueError, TypeError):
        return None


def to_iso_date(value):
    """Normalize supported dates to YYYY-MM-DD, without guessing ambiguous dates."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = _text(value)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass

    # ISO timestamps occasionally come back from models instead of date-only strings.
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return None


def validate(raw: dict) -> dict:
    """Normalize model output and flag anything that needs human review.

    A missing or suspicious field is never silently treated as verified.
    This is a format/arithmetic sanity check. A GSTIN that matches the pattern is
    unverified: it is not checked against the GSTN registry.
    """
    if not isinstance(raw, dict):
        raw = {}

    flags = []
    gstin = "".join(_text(raw.get("gstin")).upper().split())
    category_input = _text(raw.get("category"))
    category_lookup = {category.casefold(): category for category in CATEGORIES}
    category = category_lookup.get(category_input.casefold(), "Other")

    row = {
        "vendor": _text(raw.get("vendor")),
        "invoice_no": _text(raw.get("invoice_no")),
        "gstin": gstin,
        "category": category,
        "subtotal": to_num(raw.get("subtotal")),
        "tax": to_num(raw.get("tax")),
        "total": to_num(raw.get("total")),
        "date": to_iso_date(raw.get("date")),
    }

    if not row["vendor"]:
        flags.append("vendor missing or unreadable")
    if row["date"] is None:
        flags.append("date missing or unreadable")
    if row["total"] is None:
        flags.append("total missing or unreadable")
    if row["subtotal"] is None:
        flags.append("subtotal missing or unreadable")
    if row["tax"] is None:
        flags.append("tax missing or unreadable")
    if not gstin:
        flags.append("GSTIN missing or unreadable")
    elif not GSTIN_RE.fullmatch(gstin):
        flags.append("GSTIN format invalid")
    if category_input and category_input.casefold() not in category_lookup:
        flags.append("category not recognized; review category")

    if None not in (row["subtotal"], row["tax"], row["total"]):
        diff = abs(row["subtotal"] + row["tax"] - row["total"])
        if diff > AMOUNT_TOLERANCE:
            flags.append(
                f"subtotal + tax does not match total (off by {diff:.2f}; tolerance {AMOUNT_TOLERANCE:g})"
            )

    row["flags"] = flags
    return row
