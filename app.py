"""Local-first invoice auditor powered by an OpenAI-compatible vision model."""
import asyncio
import base64
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import fitz  # PyMuPDF
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from validate import CATEGORIES, validate

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("billguard")

BASE_URL = os.getenv("BASE_URL", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.getenv("API_KEY", "ollama")
MODEL = os.getenv("MODEL", "qwen2.5vl:7b")
# Comma-separated backup models. When the current model hits a daily quota, the app switches to the next.
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
_active_model = MODEL
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "15")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_PDF_PAGES = max(1, int(os.getenv("MAX_PDF_PAGES", "8")))
MAX_FILES_PER_BATCH = 30
# Rate-limit handling (HTTP 429 from hosted providers' free tiers)
MAX_RETRIES = max(0, int(os.getenv("MAX_RETRIES", "5")))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "4"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "60"))
# Minimum gap between model requests. 0 for local Ollama; set ~4-6 for free hosted tiers.
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_IMAGE_EDGE = int(os.getenv("MAX_IMAGE_EDGE", "1400"))  # smaller = fewer tokens per invoice
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
IS_LOCAL = urlparse(BASE_URL).hostname in {"localhost", "127.0.0.1", "::1"}

PROMPT = f"""You extract data from Indian invoices and receipts.
The attached images are pages of ONE invoice, in page order. Read all pages before answering.
Return ONLY one valid JSON object with exactly these keys:
{{
  "vendor": string or null,
  "invoice_no": string or null,
  "date": string as printed or null,
  "gstin": vendor's GSTIN as printed or null,
  "subtotal": number before tax/after discounts when clearly shown, otherwise null,
  "tax": total tax amount (sum of GST components if clearly shown), otherwise null,
  "total": final payable/grand total, otherwise null,
  "category": one of {", ".join(CATEGORIES)}
}}
Rules:
- Do not invent, infer, or repair unreadable numbers or GSTIN characters.
- Use JSON null for values you cannot confidently read. Do not use currency symbols in numeric fields.
- Prefer the final payable amount over a subtotal or amount due before tax.
- If a value appears on another page, use it; do not stop at page one.
- Return no markdown, commentary, or extra keys."""

app = FastAPI(
    title="BillGuard AI — Local Invoice Auditor",
    description="Extract invoice fields with a local open-weight vision model and validate them deterministically.",
    version="1.1.0",
)


class InvoiceInputError(ValueError):
    """A user-facing invoice input problem (unsupported, empty, or oversized)."""


def _png_data_url(data: bytes) -> str:
    """Normalize an image to PNG so its data-URL MIME type always matches its bytes."""
    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.width * image.height > 50_000_000:
                raise InvoiceInputError("image dimensions are too large (limit: 50 megapixels)")
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
    except InvoiceInputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvoiceInputError("image could not be decoded; try a PNG or JPEG file") from exc
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def images_for_file(name: str, data: bytes) -> list[str]:
    """Convert an invoice into page images, preserving every page of a supported PDF."""
    if not data:
        raise InvoiceInputError("file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvoiceInputError(f"file exceeds the {MAX_UPLOAD_MB} MB upload limit")

    suffix = Path(name or "").suffix.lower()
    if suffix == ".pdf":
        try:
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.needs_pass:
                    raise InvoiceInputError("PDF is password-protected; upload an unlocked copy")
                if document.page_count == 0:
                    raise InvoiceInputError("PDF has no pages")
                if document.page_count > MAX_PDF_PAGES:
                    raise InvoiceInputError(
                        f"PDF has {document.page_count} pages; limit is {MAX_PDF_PAGES} pages per file"
                    )
                images = []
                for page in document:
                    rect = page.rect
                    scale = min(2.0, MAX_IMAGE_EDGE / max(rect.width, rect.height, 1))
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                    images.append(f"data:image/png;base64,{encoded}")
                return images
        except InvoiceInputError:
            raise
        except Exception as exc:
            raise InvoiceInputError("PDF could not be read; try exporting it again") from exc

    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise InvoiceInputError("unsupported file type; use PDF, JPG, JPEG, PNG, WEBP, BMP, TIF, or TIFF")
    return [_png_data_url(data)]


def parse_model_response(content) -> dict:
    """Parse a JSON object from the model response and reject non-object output."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model returned an empty response")
    candidate = content.strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response did not contain a JSON object")
        parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


_last_request_at = 0.0
_pace_lock = asyncio.Lock()


async def _pace() -> None:
    """Space out model requests so batches don't trip provider rate limits."""
    global _last_request_at
    if MIN_REQUEST_INTERVAL <= 0:
        return
    async with _pace_lock:
        wait = _last_request_at + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def provider_message(response: httpx.Response) -> str:
    """Pull the provider's own error text (e.g. which limit was hit) out of a response."""
    try:
        err = response.json().get("error", {})
        text = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        text = response.text
    return (text or "").strip().replace("\n", " ")[:300]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor Retry-After when the provider sends it, else exponential backoff."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(max(float(header), 1.0), RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)


async def _post_with_retry(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    """POST to the model, retrying rate limits (429) and transient server errors."""
    global _active_model
    for attempt in range(MAX_RETRIES + 1):
        await _pace()
        body["model"] = _active_model
        response = await client.post(
            f"{BASE_URL}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
            # A huge Retry-After (daily quota) or an oversized request won't fix itself: fail fast.
            raw = response.headers.get("retry-after", "")
            try:
                too_long = float(raw) > 300
            except ValueError:
                too_long = False
            msg = provider_message(response).lower()
            if response.status_code == 429 and (too_long or "per day" in msg):
                remaining = [m for m in FALLBACK_MODELS if m != _active_model]
                if remaining:
                    logger.warning("Daily quota hit on %s; switching to %s", _active_model, remaining[0])
                    _active_model = remaining[0]
                    continue
            if response.status_code == 429 and (too_long or "per day" in msg or "too large" in msg):
                logger.error("Model API 429 (not retryable): %s", provider_message(response))
                response.raise_for_status()
            delay = _retry_delay(response, attempt)
            logger.warning(
                "Model API returned %s; retrying in %.0fs (attempt %d/%d). Provider says: %s",
                response.status_code, delay, attempt + 1, MAX_RETRIES, provider_message(response),
            )
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        return response
    raise RuntimeError("unreachable")  # pragma: no cover


async def extract(client: httpx.AsyncClient, name: str, data: bytes) -> dict:
    image_urls = images_for_file(name, data)
    content = [{"type": "text", "text": PROMPT}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_url}}
        for image_url in image_urls
    )
    body = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    response = await _post_with_retry(client, body)
    return parse_model_response(response.json()["choices"][0]["message"]["content"])


def error_row(message: str) -> dict:
    """Keep API rows structurally consistent even when a single file fails."""
    return {
        "vendor": "", "invoice_no": "", "date": None, "gstin": "", "category": "Other",
        "subtotal": None, "tax": None, "total": None, "flags": [message],
    }


@app.get("/health")
async def health():
    """Show whether the configured model endpoint can be reached."""
    reachable = False
    model_listed = None
    message = "Model server is not reachable. Start Ollama and check the model setup."
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{BASE_URL}/models", headers={"Authorization": f"Bearer {API_KEY}"}
            )
            response.raise_for_status()
            reachable = True
            payload = response.json()
            model_ids = [item.get("id", "") for item in payload.get("data", [])]
            model_listed = MODEL in model_ids
            message = "Model endpoint is reachable."
            if model_listed is False:
                message = "Endpoint is reachable, but the configured model was not listed; verify the model name."
    except Exception:
        logger.debug("Model health check failed", exc_info=True)

    return {
        "server_reachable": reachable,
        "model_listed": model_listed,
        "model": MODEL,
        "mode": "local" if IS_LOCAL else "remote",
        "privacy_note": (
            "Local mode: invoice images are sent only to the model endpoint on this computer."
            if IS_LOCAL else
            "Remote mode: invoice images are sent to the configured endpoint. Review that provider's privacy terms."
        ),
        "message": message,
    }


@app.post("/extract")
async def extract_many(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Choose at least one invoice file.")
    if len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"Upload at most {MAX_FILES_PER_BATCH} files per batch.")

    rows = []
    timeout = httpx.Timeout(300.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for uploaded in files:
            name = uploaded.filename or "invoice"
            try:
                # Read at most one byte above the limit; enough to reject oversized uploads.
                data = await uploaded.read(MAX_UPLOAD_BYTES + 1)
                row = validate(await extract(client, name, data))
            except InvoiceInputError as exc:
                row = error_row(str(exc))
            except httpx.ConnectError:
                row = error_row("model server unavailable; start Ollama and verify the model is installed")
            except httpx.TimeoutException:
                row = error_row("model timed out; try fewer files or a smaller PDF")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    row = error_row(f"rate limit (HTTP 429). Provider says: {provider_message(exc.response)}")
                else:
                    hint = " (API key is wrong or missing: check the API_KEY env var)" if status in (401, 403) else ""
                    row = error_row(f"model API returned HTTP {status}{hint}. Provider says: {provider_message(exc.response)}")
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                row = error_row("model returned unreadable output; try processing this file again")
            except Exception:
                logger.exception("Unexpected failure processing %s", name)
                row = error_row("unexpected processing error; see the terminal log for details")
            row["file"] = name
            rows.append(row)
            await uploaded.close()

    return {
        "model": MODEL,
        "mode": "local" if IS_LOCAL else "remote",
        "rows": rows,
    }


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
