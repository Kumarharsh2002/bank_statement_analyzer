# Bank Statement Analyzer

A local Streamlit application that extracts transaction tables from text-based bank statement PDFs and lets you search transaction descriptions for a person's name.

## Features
- Upload a bank statement PDF.
- Extract Value Date, Post Date, Details, Ref No/Cheque No, Debit, Credit and Balance.
- Search transaction descriptions by a person's name (case-insensitive; partial names work).
- Filter by date and transaction type.
- Calculate total debit and credit for the filtered results.
- Export results to CSV or Excel.

## Requirements
- Python 3.10 or newer
- A text-based PDF statement (selectable text works best)

## Installation

```bash
# Windows PowerShell
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

The app opens in your browser, usually at http://localhost:8501.

## Important limitations
Bank statement PDFs do not all use the same table layout. This starter version detects common column names and table structures, but a bank-specific parser may be needed if your bank's PDF uses unusual formatting, merged cells, or wrapped rows.

Scanned/image-only PDFs are not supported by this first version. OCR can be added with Tesseract after confirming the statement layout.

## Privacy
The app runs locally. It does not upload the PDF to an external service. Do not share bank statements containing account numbers, addresses, or other sensitive information unnecessarily.
