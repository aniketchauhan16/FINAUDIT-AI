# FinAudit AI

FinAudit AI is an AI-powered financial reconciliation system for matching messy
bank transactions against invoice records.

Phase 1 focuses on a backend MVP:

- Load transaction and invoice CSV data
- Represent records with Pydantic domain models
- Classify transactions with a LangGraph workflow
- Match transactions against invoices
- Flag ambiguous cases for human approval
- Expose review actions through FastAPI endpoints

## Current Status

- Project scaffold created
- Python dependencies listed
- Sample CSV data generator added
- Core reconciliation domain models added

## Local Setup

Create and activate a virtual environment:

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

Install dependencies:

```cmd
pip install -r requirements.txt
```

Generate sample data:

```cmd
python generate_sample_data.py
```
