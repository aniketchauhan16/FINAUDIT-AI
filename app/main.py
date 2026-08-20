from fastapi import FastAPI

from app.database import load_invoices, load_transactions
from app.models import Invoice, Transaction


app = FastAPI(
    title="FinAudit AI",
    version="0.1.0",
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/transactions", response_model=list[Transaction])
def get_transactions() -> list[Transaction]:
    return load_transactions()


@app.get("/invoices", response_model=list[Invoice])
def get_invoices() -> list[Invoice]:
    return load_invoices()