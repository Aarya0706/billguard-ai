# BillGuard AI — Local Invoice Auditor

Turn invoice photos and PDFs into a reviewable spreadsheet. BillGuard AI uses an **open-weight vision model** to extract fields, then uses deterministic Python rules to flag unreadable values, invalid GSTIN formats, and arithmetic mismatches.

## Why open innovation matters

- **Privacy by default:** the app points to Ollama on your own computer. In local mode, invoice images are sent to that local endpoint instead of a hosted AI API.
- **No per-invoice model API charge:** once the model is downloaded, local inference does not incur a provider's per-request fee. Your computer still uses power and storage.
- **Control:** you can change the model and inspect or modify the extraction prompt and validation rules.
- **A safer split of work:** the model reads messy documents; Python checks the arithmetic and data formats. Unclear values are flagged instead of silently guessed.

If you configure a remote `BASE_URL`, uploaded invoices are sent to that endpoint. Use a provider only if you have reviewed its security and privacy terms. “Local mode” does not mean the app verifies GSTINs against the government GSTN database; this project checks GSTIN **format only**.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com/) installed and running
- Enough RAM/VRAM for the model you choose. `qwen2.5vl:7b` can be resource-intensive; a smaller compatible vision model may suit a modest laptop better.

## Run locally on Windows (PowerShell)

From the folder containing `app.py`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
ollama pull qwen2.5vl:7b
uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000**. Keep Ollama running in the background. The first model download needs internet; after the model is installed, invoice extraction can run locally without sending images to a cloud model provider.

If PowerShell blocks virtual-environment activation, run the install command with your Python executable or use Command Prompt activation (`.venv\Scripts\activate.bat`).

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ollama pull qwen2.5vl:7b
uvicorn app:app --host 127.0.0.1 --port 8000
```

## Use a different model or endpoint

The default is local Ollama at `http://localhost:11434/v1`. Set the model name to a vision model supported by your OpenAI-compatible endpoint.

PowerShell example for a **remote** provider (invoice data leaves your machine):

```powershell
$env:BASE_URL = "https://your-provider.example/v1"
$env:API_KEY = "your-api-key"
$env:MODEL = "provider-model-id"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Do not commit API keys or real invoices to Git. The app does not read a `.env` file automatically; these variables must be set in the shell where you start Uvicorn.

## What it checks

1. Sends all pages of each PDF (up to 8 pages) or a supported image to the configured vision model.
2. Extracts vendor, invoice number, date, GSTIN, subtotal, tax, total, and a category.
3. Normalizes common date and currency formats.
4. Flags missing/unreadable key fields, invalid GSTIN **format**, unrecognized categories, and cases where subtotal + tax differs from total by more than ₹1.
5. Shows a review table and totals by category, with a CSV download.

Supported uploads: PDF, JPG/JPEG, PNG, WEBP, BMP, TIF/TIFF. Default limits are 15 MB per file, 8 pages per PDF, and 30 files per batch. These can be changed with `MAX_UPLOAD_MB` and `MAX_PDF_PAGES` environment variables.

## Run tests

```bash
python -m unittest discover -s tests -v
```

The tests cover GSTIN format checks, date/currency normalization, arithmetic mismatch flags, image normalization, and multi-page PDF handling. They do not require a running model server.

## Limitations

- This is a small-business assistant, not accounting, tax, or legal advice. Always compare flagged values with the original document.
- OCR/vision quality depends on image clarity and the chosen model. Never assume extracted data is correct just because no rule fired.
- GSTIN is checked for pattern only, not against the live GSTN registry or its checksum.
- The code is MIT-licensed. The model, Ollama, and other dependencies have their own licenses and terms; review those separately for commercial use.

## Stack

Python · FastAPI · Ollama/OpenAI-compatible API · an open-weight vision model · PyMuPDF · Pillow · HTML/CSS/JavaScript
