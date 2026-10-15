# BillGuard AI — Invoice Auditor

Turn invoice photos and PDFs into a reviewable spreadsheet. BillGuard AI uses a **vision model** to extract fields, then uses deterministic Python rules to flag unreadable values, invalid GSTIN formats, and arithmetic mismatches.

It works with any OpenAI-compatible vision endpoint: a local open-weight model through Ollama (default), or a hosted provider such as Google Gemini or Groq.

## Why this design

- **Privacy by default:** the app points to Ollama on your own computer. In local mode, invoice images never leave your machine.
- **No per-invoice API charge in local mode:** once the model is downloaded, local inference has no per-request fee. Your computer still uses power and storage.
- **Swappable model:** change `BASE_URL`, `API_KEY` and `MODEL` to use a different model or provider without touching the code.
- **A safer split of work:** the model reads messy documents; Python checks the arithmetic and data formats. Unclear values are flagged instead of silently guessed.

If you configure a remote `BASE_URL`, uploaded invoices are sent to that endpoint. Use a provider only if you have reviewed its security and privacy terms. This project checks GSTIN **format only**; it does not verify GSTINs against the government GSTN database.

## Requirements

- Python 3.10 or newer
- **Either** [Ollama](https://ollama.com/) installed and running (local mode), **or** an API key for an OpenAI-compatible vision endpoint (remote mode)
- For local mode, enough RAM/VRAM for the model you choose. `qwen2.5vl:7b` can be resource-intensive; a smaller compatible vision model may suit a modest laptop better.

## Run locally with Ollama (Windows PowerShell)

From the folder containing `app.py`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
ollama pull qwen2.5vl:7b
uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000**. Keep Ollama running in the background. The first model download needs internet; after that, extraction runs locally.

If PowerShell blocks virtual-environment activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` first, or use Command Prompt activation (`.venv\Scripts\activate.bat`).

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ollama pull qwen2.5vl:7b
uvicorn app:app --host 127.0.0.1 --port 8000
```

## Use a hosted model instead

Set the endpoint, key and a **vision-capable** model name in the same terminal where you start Uvicorn. The app does not read a `.env` file, and these variables only last for that terminal session.

**Google Gemini (free tier available):**

```powershell
$env:BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
$env:API_KEY = "your-gemini-api-key"
$env:MODEL = "gemini-3.8-flash"
$env:MIN_REQUEST_INTERVAL = "4"
uvicorn app:app --host 127.0.0.1 --port 8000
```

**Groq:**

```powershell
$env:BASE_URL = "https://api.groq.com/openai/v1"
$env:API_KEY = "your-groq-api-key"
$env:MODEL = "your-groq-vision-model-id"
$env:MIN_REQUEST_INTERVAL = "8"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Model names change over time and differ per account. To list the models your key can use:

```powershell
(Invoke-RestMethod -Uri "$env:BASE_URL/models" -Headers @{Authorization="Bearer $env:API_KEY"}).data.id
```

Pick one that supports image input, and confirm the exact ID in your provider's model documentation. `/health` ignores a leading `models/` prefix; add `?probe=true` (e.g. `http://127.0.0.1:8000/health?probe=true`) to send a 1-token test request. This uses a little quota.

Do not commit API keys or real invoices to Git.

## Configuration

All settings are environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `API_KEY` | none | API key for remote providers |
| `MODEL` | `qwen2.5vl:7b` | Vision model name |
| `FALLBACK_MODELS` | empty | Comma-separated backup models; the app switches to the next one when the current model hits a daily quota |
| `MIN_REQUEST_INTERVAL` | `0` | Minimum seconds between model requests. Use `0` for local Ollama; `4`–`20` for free hosted tiers |
| `MAX_RETRIES` | `5` | Retries for rate limits (429) and temporary server errors (5xx) |
| `RETRY_BASE_DELAY` | `4` | First retry wait in seconds; doubles each attempt |
| `RETRY_MAX_DELAY` | `60` | Longest single retry wait in seconds |
| `MAX_IMAGE_EDGE` | `1400` | Longest image side sent to the model, in pixels. Smaller means fewer tokens per invoice |
| `MAX_UPLOAD_MB` | `15` | Maximum size per file |
| `MAX_PDF_PAGES` | `8` | Maximum pages read per PDF |

## What it does

1. Processes files **one at a time**, so results appear row by row and one failure never loses the whole batch.
2. Sends all pages of each PDF (up to `MAX_PDF_PAGES`) or a supported image to the vision model.
3. Extracts vendor, invoice number, date, GSTIN, subtotal, tax, total, and a category.
4. Normalizes common date and currency formats.
5. Flags missing or unreadable key fields, invalid GSTIN **format**, unrecognized categories, and cases where subtotal + tax differs from total by more than `AMOUNT_TOLERANCE` (default ₹1).
6. Shows a review table and totals by category, with a CSV download.

Supported uploads: PDF, JPG/JPEG, PNG, WEBP, BMP, TIF/TIFF. Up to 30 files per batch.

## Rate limits and errors

Hosted free tiers limit requests and tokens, so the app is built to handle that:

- **HTTP 429 / 5xx:** retried automatically with backoff, honoring the provider's `Retry-After` time.
- **Daily quota reached:** the app stops retrying (waiting would not help), marks that model as exhausted for `QUOTA_COOLDOWN` seconds, and tries each model in `FALLBACK_MODELS` in order. When all are exhausted, remaining files fail immediately with a "try again later" message and send no further requests.
- **Long waits:** if the provider asks for a wait that would exceed `MAX_TOTAL_WAIT`, the row says so instead of waiting.
- **Failed rows** show the provider's error text in the Result column. Re-upload only those files later.
- **HTTP 401/403:** the API key is wrong or missing.
- **Connection interrupted:** the page retries each file up to 3 times. Keep the tab open and the computer awake while a batch runs.

## Run tests

```bash
python -m unittest discover -s tests -v
```

The tests cover GSTIN format checks, date/currency normalization, arithmetic mismatch flags, image normalization, multi-page PDF handling, and API routes. They do not require a running model server.

## Limitations

- This is a small-business assistant, not accounting, tax, or legal advice. Always compare flagged values with the original document.
- Extraction quality depends on image clarity and the chosen model. Never assume extracted data is correct just because no rule fired.
- GSTIN is checked for pattern only, not against the live GSTN registry or its checksum.
- Results are held in the browser page only. Download the CSV before refreshing or closing the tab.
- The code is MIT-licensed. The model, Ollama, hosted providers and other dependencies have their own licenses and terms; review those separately for commercial use.

## Stack

Python · FastAPI · Ollama / OpenAI-compatible API · vision model (open-weight locally, or Gemini/Groq) · PyMuPDF · Pillow · HTML/CSS/JavaScript
